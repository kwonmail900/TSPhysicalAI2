# ESP32 노드 펌웨어

대상은 **Classic ESP32 WROOM-32 / ESP32 Dev Module**입니다. ESP32-S3/C3의 핀 배치·코어 수에 맞춰 검증한 펌웨어가 아닙니다.

## 설치와 업로드

Arduino IDE의 보드 관리자에서 **esp32 by Espressif Systems 3.x**를 설치합니다. 라이브러리 관리자에서 다음을 설치합니다.

- Adafruit INA260 Library와 의존성 Adafruit BusIO
- ArduinoJson **7.x**

`esp32_node/config.h.example`을 같은 폴더의 `config.h`로 복사하고 Wi-Fi 이름·비밀번호, PC의 LAN 주소, PC와 같은 장치 토큰을 입력합니다. `config.h`는 Git에 올리지 않습니다. PC의 `127.0.0.1`은 ESP32가 접속할 주소가 아닙니다.

Arduino IDE에서 `esp32_node/esp32_node.ino`를 열고 **ESP32 Dev Module**과 연결한 포트를 선택하여 컴파일·업로드합니다. 이 스케치는 Arduino-ESP32 3.x의 `ledcAttach(pin, frequency, resolution)`과 `ledcWrite(pin, duty)` API를 사용합니다. 2.x용 LEDC API와 혼용하지 않습니다.

CLI를 사용할 경우의 명령 예시는 다음과 같습니다. 인덱스·패키지의 서명과 체크섬 검증을 유지하십시오.

```bash
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.3.2 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli lib install "Adafruit INA260 Library@1.5.3" "ArduinoJson@7.4.2"
arduino-cli compile --fqbn esp32:esp32:esp32 --jobs 2 firmware/esp32_node
```

위 명령은 GitHub Actions의 버전과 일치합니다. 보드 빌드와 실물 시험의 검증 상태는 [검증 기록](../docs/VALIDATION.md)에서 확인합니다. 아래 네이티브 검증은 Arduino 보드 전체 빌드나 실물 시험을 대신하지 않습니다.

## 핀과 데이터

| 기능 | ESP32 핀 | 연결 조건 |
|---|---|---|
| INA260 SDA / SCL | GPIO21 / GPIO22 | 센서 논리 전원 3.3V |
| LED 제어 | GPIO25 | 드라이버 신호 입력, 500Hz PWM |
| 모터 제어 | GPIO27 | DRV8871 IN1, IN2는 GND |
| 엔코더 A / B | GPIO32 / GPIO33 | 엔코더 전원 3.3V, 모터 전원과 별도 |

센서·드라이버·ESP32·12V 전원의 GND를 공통으로 연결합니다. GPIO는 제어 신호이며 LED와 모터의 전원을 공급하지 않습니다. 전체 전력 배선은 [하드웨어 문서](../docs/HARDWARE.md)를 확인합니다.

INA260의 전압 mV, 전류 mA, 전력 mW는 각각 V, A, W로 바꿔 전송합니다. USB에서 공급하는 ESP32 자체 소비전력은 부하 측 INA260 계측에 포함되지 않습니다.

엔코더는 두 채널의 상승·하강 에지를 모두 계수합니다. `ENCODER_COUNTS_PER_OUTPUT_REV`는 **출력축 한 회전당 전체 에지 수**입니다. 예시 1806.96은 12 counts/motor-rev와 약 150.58:1 기어비를 가진 모터의 계산값이므로 납품 모터의 정확한 명세와 실제 출력축 회전으로 검증합니다. 전진 시 음수로 계수되면 부하 전원을 끄고 `ENCODER_SIGN`을 조정합니다.

역회전 명령은 지원하지 않습니다. 완료 작업량은 부팅 이후 부호를 보정한 엔코더 누적 순변위의 전진 최고값이며, 단순 PWM 명령량을 작업 완료로 간주하지 않습니다. RPM은 100ms 표본 구간에서 계산합니다. 목표 회전수에서의 정지에는 센서 표본 주기와 제어 주기에 따른 오차가 있습니다.

## 로컬 정지와 재가동

- 부팅 직후 출력은 0입니다.
- 통신 태스크와 별도의 제어 루프가 명령 유효시간·전압·전류·전력·회전수·정체를 확인합니다.
- 명령은 1~2000ms 유효기간만 허용하며 기본 PC 명령은 1500ms입니다. 새 명령이 도착하지 않으면 출력이 정지하고 오류가 유지됩니다.
- 동일하거나 오래된 `command_id`는 유효시간을 연장하지 않습니다. ID는 64비트로 처리합니다.
- `reset_fault`는 `arm=false`, 두 출력 0인 별도 명령입니다. 현재 센서 조건이 유효할 때 오류만 해제하며, 다음 별도 ARM 명령이 있어야 재가동합니다.
- 네트워크 응답이 느려져도 HTTP 대기가 제어 루프를 막지 않습니다. 20ms는 설정한 루프 대기 주기이며, 센서 I²C 처리 시간을 포함한 하드 실시간 보장은 아닙니다.
- 부팅 당시 센서 미확인으로 `sensor_invalid`가 유지되면 정상 계측을 확인한 뒤 오류 해제와 ARM을 순서대로 수행합니다.

2A 노드 과전류 한계는 모터 정체 검출을 보장하지 않습니다. 선택한 소형 모터의 정체 전류가 2A보다 작을 수 있어, PWM 25% 초과·기동 유예 이후 저속 지속 조건의 별도 정체 검출을 적용했습니다. 실제 임계값·기동시간은 무부하 교정과 정격 확인 후 조정합니다. 소프트웨어 검출은 퓨즈·드라이버의 전류 제한과 회전부 가드를 대신하지 않습니다.

## 네이티브 보호 로직 검증

프로젝트 루트에서 다음을 실행합니다.

```bash
g++ -std=c++17 -Wall -Wextra -Werror -pedantic tests/native_firmware_test.cpp -o /tmp/tspai-firmware-guards
/tmp/tspai-firmware-guards
```

현재 10개 검증에는 초기 정지, 명령 만료, 재전송, 비상정지 유지, 현재 조건을 확인한 오류 해제, 별도 ARM, 잘못된 값, 목표 회전수 정지, 모터 정체 및 32비트 밀리초 경계를 넘는 시간이 포함됩니다.
