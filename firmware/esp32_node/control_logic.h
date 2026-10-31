#pragma once

// Hardware-independent local limits. Positive motor duty is forward only.
// Compile tests with: g++ -std=c++17 -Wall -Wextra -pedantic tests/native_firmware_test.cpp -o /tmp/tspai-guards
#include <cmath>
#include <cstdint>

namespace tspai {

struct Limits {
  double min_voltage_v = 9.0;
  double max_voltage_v = 15.0;
  double max_current_a = 2.0;
  double max_power_w = 30.0;
  double max_rpm = 400.0;
  double stall_min_duty = 0.25;
  double stall_min_rpm = 5.0;
  uint64_t motor_start_grace_ms = 1500;
  uint64_t stall_duration_ms = 1500;
  uint32_t max_ttl_ms = 2000;
};

struct Sensor {
  bool valid = false;
  double voltage_v = 0;
  double current_a = 0;
  double power_w = 0;
  double rpm = 0;
  double total_revolutions = 0;
};

struct Command {
  uint64_t command_id = 0;
  double led_duty = 0;
  double motor_duty = 0;
  uint32_t ttl_ms = 0;
  bool arm = false;
  bool reset_fault = false;
  bool emergency_stop = false;
  double target_revolutions = 0;
};

enum class Fault {
  None, SensorInvalid, Voltage, Current, Power, Speed, CommandInvalid,
  CommandTimeout, EmergencyStop, MotorStall, EncoderDirection
};

inline const char* faultName(Fault value) {
  switch (value) {
    case Fault::None: return "";
    case Fault::SensorInvalid: return "sensor_invalid";
    case Fault::Voltage: return "voltage_limit";
    case Fault::Current: return "current_limit";
    case Fault::Power: return "power_limit";
    case Fault::Speed: return "rpm_limit";
    case Fault::CommandInvalid: return "invalid_command";
    case Fault::CommandTimeout: return "command_timeout";
    case Fault::EmergencyStop: return "emergency_stop";
    case Fault::MotorStall: return "motor_stall";
    case Fault::EncoderDirection: return "encoder_direction";
  }
  return "unknown_fault";
}

inline bool finiteNonnegative(double value) {
  return std::isfinite(value) && value >= 0;
}

class Guard {
 public:
  explicit Guard(Limits limits = Limits()) : limits_(limits) {}

  Fault sensorFault(const Sensor& sensor) const {
    if (!sensor.valid || !finiteNonnegative(sensor.voltage_v) ||
        !finiteNonnegative(sensor.current_a) || !finiteNonnegative(sensor.power_w) ||
        !finiteNonnegative(sensor.rpm) || !finiteNonnegative(sensor.total_revolutions)) {
      return Fault::SensorInvalid;
    }
    if (sensor.voltage_v < limits_.min_voltage_v || sensor.voltage_v > limits_.max_voltage_v) return Fault::Voltage;
    if (sensor.current_a > limits_.max_current_a) return Fault::Current;
    if (sensor.power_w > limits_.max_power_w) return Fault::Power;
    if (sensor.rpm > limits_.max_rpm) return Fault::Speed;
    return Fault::None;
  }

  static bool validCommand(const Command& command, uint32_t max_ttl_ms) {
    return command.command_id > 0 && command.ttl_ms > 0 && command.ttl_ms <= max_ttl_ms &&
        finiteNonnegative(command.led_duty) && command.led_duty <= 1.0 &&
        finiteNonnegative(command.motor_duty) && command.motor_duty <= 1.0 &&
        finiteNonnegative(command.target_revolutions) && command.target_revolutions <= 1e12 &&
        !(command.reset_fault && (command.arm || command.led_duty > 0 || command.motor_duty > 0));
  }

  bool accept(const Command& command, uint64_t now_ms, const Sensor& sensor) {
    if (!validCommand(command, limits_.max_ttl_ms)) {
      latch(Fault::CommandInvalid);
      return false;
    }
    // A replay never extends the original deadline or re-enables an output.
    if (command.command_id <= acknowledged_id_) return false;
    acknowledged_id_ = command.command_id;
    expires_ms_ = now_ms + command.ttl_ms;
    if (command.emergency_stop) {
      latch(Fault::EmergencyStop);
      return true;
    }
    if (command.reset_fault) {
      stopOutputs();
      const Fault active = sensorFault(sensor);
      fault_ = active;  // Reset succeeds only while measured conditions are valid.
      return true;      // A separate command must explicitly ARM afterwards.
    }
    if (!command.arm) {
      stopOutputs();
      return true;
    }
    if (fault_ != Fault::None) {
      stopOutputs();
      return true;
    }
    requested_led_ = command.led_duty;
    requested_motor_ = command.motor_duty;
    target_revolutions_ = command.target_revolutions;
    armed_ = true;
    tick(sensor, now_ms);
    return true;
  }

  void tick(const Sensor& sensor, uint64_t now_ms) {
    const Fault active = sensorFault(sensor);
    if (active != Fault::None) latch(active);
    if (!armed_ || fault_ != Fault::None) {
      led_duty_ = motor_duty_ = 0;
      return;
    }
    if (now_ms >= expires_ms_) {
      latch(Fault::CommandTimeout);
      return;
    }
    const double next_motor = sensor.total_revolutions >= target_revolutions_ ? 0 : requested_motor_;
    if (next_motor > limits_.stall_min_duty) {
      if (motor_duty_ <= limits_.stall_min_duty) {
        motor_start_ms_ = now_ms;
        low_speed_since_ms_ = 0;
        low_speed_tracking_ = false;
      }
      if (now_ms - motor_start_ms_ >= limits_.motor_start_grace_ms) {
        if (sensor.rpm < limits_.stall_min_rpm) {
          if (!low_speed_tracking_) {
            low_speed_since_ms_ = now_ms;
            low_speed_tracking_ = true;
          } else if (now_ms - low_speed_since_ms_ >= limits_.stall_duration_ms) {
            latch(Fault::MotorStall);
            return;
          }
        } else {
          low_speed_tracking_ = false;
        }
      }
    } else {
      low_speed_tracking_ = false;
    }
    led_duty_ = requested_led_;
    motor_duty_ = next_motor;
  }

  void latch(Fault fault) {
    if (fault_ == Fault::None) fault_ = fault;
    stopOutputs();
  }

  bool armed() const { return armed_; }
  Fault fault() const { return fault_; }
  double ledDuty() const { return led_duty_; }
  double motorDuty() const { return motor_duty_; }
  uint64_t acknowledgedId() const { return acknowledged_id_; }

 private:
  void stopOutputs() {
    armed_ = false;
    led_duty_ = motor_duty_ = 0;
    requested_led_ = requested_motor_ = 0;
    low_speed_tracking_ = false;
  }

  Limits limits_;
  bool armed_ = false;
  Fault fault_ = Fault::None;
  uint64_t acknowledged_id_ = 0;
  uint64_t expires_ms_ = 0;
  double requested_led_ = 0;
  double requested_motor_ = 0;
  double target_revolutions_ = 0;
  double led_duty_ = 0;
  double motor_duty_ = 0;
  uint64_t motor_start_ms_ = 0;
  uint64_t low_speed_since_ms_ = 0;
  bool low_speed_tracking_ = false;
};

}  // namespace tspai
