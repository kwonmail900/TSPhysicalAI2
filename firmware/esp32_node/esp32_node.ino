// ESP32 Classic WROOM-32, Arduino-ESP32 3.x.
// Libraries: Adafruit INA260 (+ Adafruit BusIO), ArduinoJson 7.x.
// Copy config.h.example to config.h and enter the local teaching-LAN settings.
#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Adafruit_INA260.h>
#include <esp_timer.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include "config.h"
#include "control_logic.h"

using tspai::Command;
using tspai::Fault;
using tspai::Sensor;

struct QueuedCommand { bool valid; Command command; };
struct Telemetry {
  Sensor sensor;
  uint64_t uptime_ms;
  uint64_t command_ack;
  bool armed;
  Fault fault;
  double led_applied_duty;
  double motor_applied_duty;
};

Adafruit_INA260 ina260;
tspai::Guard* guard = nullptr;
QueueHandle_t command_queue = nullptr;
portMUX_TYPE encoder_mux = portMUX_INITIALIZER_UNLOCKED;
portMUX_TYPE telemetry_mux = portMUX_INITIALIZER_UNLOCKED;
volatile int64_t encoder_ticks = 0;
volatile uint8_t previous_encoder = 0;
Telemetry shared_telemetry{};
char boot_id[17]{};
bool sensor_ready = false;
bool pwm_ready = false;
bool configuration_ready = true;
DRAM_ATTR static const int8_t encoder_transitions[16] = {0, -1, 1, 0, 1, 0, 0, -1, -1, 0, 0, 1, 0, 1, -1, 0};

static uint64_t uptimeMs() {
  return static_cast<uint64_t>(esp_timer_get_time()) / 1000ULL;
}

void ARDUINO_ISR_ATTR onEncoderEdge() {
  const uint8_t state = static_cast<uint8_t>((digitalRead(ENCODER_A_PIN) << 1) | digitalRead(ENCODER_B_PIN));
  // Both channels on CHANGE: all four quadrature edges are counted.
  portENTER_CRITICAL_ISR(&encoder_mux);
  encoder_ticks += encoder_transitions[(previous_encoder << 2) | state] * ENCODER_SIGN;
  previous_encoder = state;
  portEXIT_CRITICAL_ISR(&encoder_mux);
}

static bool parseCommand(const String& body, Command& output) {
  JsonDocument doc;
  if (deserializeJson(doc, body)) return false;
  JsonObjectConst obj = doc["command"].is<JsonObject>() ? doc["command"].as<JsonObjectConst>() : doc.as<JsonObjectConst>();
  if (obj.isNull() || !obj["command_id"].is<uint64_t>() || !obj["ttl_ms"].is<uint32_t>() ||
      !obj["arm"].is<bool>() || !obj["reset_fault"].is<bool>() || !obj["emergency_stop"].is<bool>() ||
      !obj["led_duty"].is<double>() || !obj["motor_duty"].is<double>() || !obj["target_revolutions"].is<double>()) {
    return false;
  }
  output.command_id = obj["command_id"].as<uint64_t>();
  output.led_duty = obj["led_duty"].as<double>();
  output.motor_duty = obj["motor_duty"].as<double>();
  output.ttl_ms = obj["ttl_ms"].as<uint32_t>();
  output.arm = obj["arm"].as<bool>();
  output.reset_fault = obj["reset_fault"].as<bool>();
  output.emergency_stop = obj["emergency_stop"].as<bool>();
  output.target_revolutions = obj["target_revolutions"].as<double>();
  return tspai::Guard::validCommand(output, 2000);
}

static void communicationsTask(void*) {
  uint64_t sequence = 0;
  uint64_t last_wifi_attempt = 0;
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      if (uptimeMs() - last_wifi_attempt >= 5000 || last_wifi_attempt == 0) {
        WiFi.disconnect();
        WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
        last_wifi_attempt = uptimeMs();
      }
      vTaskDelay(pdMS_TO_TICKS(100));
      continue;
    }
    Telemetry snapshot;
    portENTER_CRITICAL(&telemetry_mux);
    snapshot = shared_telemetry;
    portEXIT_CRITICAL(&telemetry_mux);
    JsonDocument request;
    request["device_id"] = DEVICE_ID;
    request["boot_id"] = boot_id;
    request["seq"] = ++sequence;
    request["uptime_ms"] = snapshot.uptime_ms;
    request["voltage_v"] = snapshot.sensor.voltage_v;
    request["current_a"] = snapshot.sensor.current_a;
    request["power_w"] = snapshot.sensor.power_w;
    request["rpm"] = snapshot.sensor.rpm;
    request["total_revolutions"] = snapshot.sensor.total_revolutions;
    request["fault"] = tspai::faultName(snapshot.fault);
    request["armed"] = snapshot.armed;
    request["command_ack"] = snapshot.command_ack;
    request["led_applied_duty"] = snapshot.led_applied_duty;
    request["motor_applied_duty"] = snapshot.motor_applied_duty;
    String payload;
    serializeJson(request, payload);
    WiFiClient client;
    HTTPClient http;
    // HTTP work is confined to this task: it cannot suspend the 20 ms control loop.
    http.setConnectTimeout(300);
    http.setTimeout(300);
    if (http.begin(client, String(SERVER_BASE_URL) + "/api/device/exchange")) {
      http.addHeader("Content-Type", "application/json");
      http.addHeader("X-Device-Token", DEVICE_TOKEN);
      const int code = http.POST(payload);
      if (code == 200) {
        QueuedCommand incoming{};
        if (http.getSize() <= 2048) {
          const String body = http.getString();
          incoming.valid = body.length() <= 2048 && parseCommand(body, incoming.command);
        }
        // Invalid command responses fail closed; connection failures simply expire TTL.
        xQueueOverwrite(command_queue, &incoming);
      }
      http.end();
    }
    vTaskDelay(pdMS_TO_TICKS(EXCHANGE_PERIOD_MS));
  }
}

static void writeOutputs() {
  const uint32_t maximum = (1UL << PWM_BITS) - 1;
  if (pwm_ready) {
    ledcWrite(LED_PIN, static_cast<uint32_t>(guard->ledDuty() * maximum + 0.5));
    ledcWrite(MOTOR_PIN, static_cast<uint32_t>(guard->motorDuty() * maximum + 0.5));
  } else {
    digitalWrite(LED_PIN, LOW);
    digitalWrite(MOTOR_PIN, LOW);
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  pinMode(MOTOR_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  digitalWrite(MOTOR_PIN, LOW);
  tspai::Limits limits;
  limits.min_voltage_v = MIN_VOLTAGE_V;
  limits.max_voltage_v = MAX_VOLTAGE_V;
  limits.max_current_a = MAX_CURRENT_A;
  limits.max_power_w = MAX_POWER_W;
  limits.max_rpm = MAX_MOTOR_RPM;
  static tspai::Guard local_guard(limits);
  guard = &local_guard;
  const bool led_ok = ledcAttach(LED_PIN, PWM_FREQUENCY_HZ, PWM_BITS);
  const bool motor_ok = ledcAttach(MOTOR_PIN, PWM_FREQUENCY_HZ, PWM_BITS);
  pwm_ready = led_ok && motor_ok;
  configuration_ready = pwm_ready && std::isfinite(ENCODER_COUNTS_PER_OUTPUT_REV) &&
      ENCODER_COUNTS_PER_OUTPUT_REV > 0 && (ENCODER_SIGN == 1 || ENCODER_SIGN == -1);
  if (!configuration_ready) {
    guard->latch(Fault::CommandInvalid);
  }
  pinMode(ENCODER_A_PIN, INPUT_PULLUP);
  pinMode(ENCODER_B_PIN, INPUT_PULLUP);
  previous_encoder = (digitalRead(ENCODER_A_PIN) << 1) | digitalRead(ENCODER_B_PIN);
  attachInterrupt(digitalPinToInterrupt(ENCODER_A_PIN), onEncoderEdge, CHANGE);
  attachInterrupt(digitalPinToInterrupt(ENCODER_B_PIN), onEncoderEdge, CHANGE);
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setTimeOut(25);
  sensor_ready = ina260.begin(0x40, &Wire);
  snprintf(boot_id, sizeof(boot_id), "%08lx%08lx", static_cast<unsigned long>(esp_random()), static_cast<unsigned long>(esp_random()));
  command_queue = xQueueCreate(1, sizeof(QueuedCommand));
  if (!command_queue) {
    configuration_ready = false;
    guard->latch(Fault::CommandInvalid);
    Serial.println("Command queue allocation failed: outputs remain off.");
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  if (xTaskCreatePinnedToCore(communicationsTask, "http_exchange", 8192, nullptr, 1, nullptr, 0) != pdPASS) {
    configuration_ready = false;
    guard->latch(Fault::CommandInvalid);
  }
  Serial.println("TSPhysicalAI2 node ready. Outputs remain OFF until explicit reset/ARM.");
}

void loop() {
  static Sensor sensor;
  static uint64_t previous_sample_ms = 0;
  static int64_t previous_ticks = 0;
  static double forward_highwater = 0;
  const uint64_t now = uptimeMs();
  if (now - previous_sample_ms >= SENSOR_PERIOD_MS) {
    int64_t ticks;
    portENTER_CRITICAL(&encoder_mux);
    ticks = encoder_ticks;
    portEXIT_CRITICAL(&encoder_mux);
    const uint64_t elapsed = now - previous_sample_ms;
    const double signed_rpm = (ticks - previous_ticks) * 60000.0 / (ENCODER_COUNTS_PER_OUTPUT_REV * elapsed);
    previous_sample_ms = now;
    previous_ticks = ticks;
    forward_highwater = fmax(forward_highwater, ticks / ENCODER_COUNTS_PER_OUTPUT_REV);
    sensor.valid = sensor_ready;
    sensor.voltage_v = sensor_ready ? ina260.readBusVoltage() / 1000.0 : 0;
    sensor.current_a = sensor_ready ? ina260.readCurrent() / 1000.0 : 0;
    sensor.power_w = sensor_ready ? ina260.readPower() / 1000.0 : 0;
    sensor.rpm = fabs(signed_rpm);
    sensor.total_revolutions = forward_highwater;
    if (signed_rpm < -5 && guard->motorDuty() > 0.25) guard->latch(Fault::EncoderDirection);
  }
  if (command_queue) {
    QueuedCommand incoming{};
    if (xQueueReceive(command_queue, &incoming, 0) == pdTRUE) {
      if (incoming.valid) guard->accept(incoming.command, now, sensor);
      else guard->latch(Fault::CommandInvalid);
    }
  }
  if (!configuration_ready) guard->latch(Fault::CommandInvalid);
  guard->tick(sensor, now);
  writeOutputs();
  Telemetry snapshot{sensor, now, guard->acknowledgedId(), guard->armed(), guard->fault(), guard->ledDuty(), guard->motorDuty()};
  portENTER_CRITICAL(&telemetry_mux);
  shared_telemetry = snapshot;
  portEXIT_CRITICAL(&telemetry_mux);
  delay(CONTROL_PERIOD_MS);
}
