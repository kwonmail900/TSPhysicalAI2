#include <cassert>
#include <cmath>
#include <iostream>
#include <limits>
#include "../firmware/esp32_node/control_logic.h"

using namespace tspai;

static Sensor sensor() { return {true, 12.0, 0.2, 2.4, 80.0, 0.0}; }
static Command run(uint64_t id) { return {id, 0.5, 0.6, 1500, true, false, false, 100}; }

int main() {
  int tests = 0;
  {
    Guard g;
    g.tick(sensor(), 0);
    assert(!g.armed() && g.ledDuty() == 0 && g.motorDuty() == 0);
    assert(g.fault() == Fault::None);
    ++tests;
  }
  {
    Guard g;
    auto s = sensor();
    assert(g.accept(run(1), 100, s));
    assert(g.armed() && g.motorDuty() == .6 && g.ledDuty() == .5);
    assert(!g.accept(run(1), 1400, s));
    g.tick(s, 1600);
    assert(!g.armed() && g.motorDuty() == 0 && g.ledDuty() == 0);
    assert(g.fault() == Fault::CommandTimeout);
    ++tests;
  }
  {
    Guard g;
    auto s = sensor();
    auto c = run(1); c.emergency_stop = true;
    assert(g.accept(c, 0, s));
    assert(g.fault() == Fault::EmergencyStop && !g.armed());
    g.accept(run(2), 1, s);
    assert(!g.armed());
    c = {}; c.command_id = 3; c.ttl_ms = 1500; c.reset_fault = true;
    assert(g.accept(c, 2, s));
    assert(g.fault() == Fault::None && !g.armed());
    assert(g.accept(run(4), 3, s) && g.armed());
    ++tests;
  }
  {
    Guard g;
    auto s = sensor(); s.current_a = 2.1;
    g.tick(s, 0);
    assert(g.fault() == Fault::Current);
    Command c{}; c.command_id = 1; c.ttl_ms = 1000; c.reset_fault = true;
    g.accept(c, 1, s);
    assert(g.fault() == Fault::Current && !g.armed());
    s = sensor(); c.command_id = 2;
    g.accept(c, 2, s);
    assert(g.fault() == Fault::None && !g.armed());
    ++tests;
  }
  {
    Guard g;
    auto s = sensor();
    auto c = run(1); c.motor_duty = std::numeric_limits<double>::quiet_NaN();
    assert(!g.accept(c, 0, s));
    assert(g.fault() == Fault::CommandInvalid && !g.armed());
    ++tests;
  }
  {
    Guard g;
    auto s = sensor();
    auto c = run(1); c.ttl_ms = 2500;
    assert(!g.accept(c, 0, s) && g.fault() == Fault::CommandInvalid);
    ++tests;
  }
  {
    Guard g;
    auto s = sensor();
    g.accept(run(1), 0, s);
    s.total_revolutions = 100;
    g.tick(s, 10);
    assert(g.armed() && g.motorDuty() == 0 && g.ledDuty() == .5);
    ++tests;
  }
  {
    Guard g;
    auto s = sensor(); s.rpm = 0;
    for (uint64_t t = 0; t <= 3500; t += 100) {
      if (t % 500 == 0) g.accept(run(t / 500 + 1), t, s);
      g.tick(s, t);
    }
    assert(g.fault() == Fault::MotorStall && !g.armed());
    ++tests;
  }
  {
    Guard g;
    auto s = sensor(); s.voltage_v = std::numeric_limits<double>::infinity();
    g.tick(s, 0);
    assert(g.fault() == Fault::SensorInvalid);
    ++tests;
  }
  {
    Guard g;
    auto s = sensor();
    const uint64_t beyond_millis_wrap = (uint64_t{1} << 32) + 100;
    g.accept(run(1), beyond_millis_wrap, s);
    g.tick(s, beyond_millis_wrap + 1499);
    assert(g.armed());
    g.tick(s, beyond_millis_wrap + 1500);
    assert(!g.armed());
    ++tests;
  }
  std::cout << "Native firmware guard checks passed: " << tests << '\n';
}
