import unittest
from unittest.mock import patch

from control import LabController


def packet(seq=0, uptime_ms=0, boot_id="boot-a", power=6, revs=0, **extra):
    result = {"device_id": "node-1", "boot_id": boot_id, "seq": seq,
              "uptime_ms": uptime_ms, "voltage_v": 12, "current_a": power / 12,
              "power_w": power, "rpm": 0, "total_revolutions": revs,
              "fault": "", "armed": False, "command_ack": 0}
    result.update(extra)
    return result


class ControlTests(unittest.TestCase):
    def test_start_disarmed_and_hardware_requires_telemetry(self):
        hardware = LabController("hardware")
        self.assertFalse(hardware.status()["armed"])
        with self.assertRaises(ValueError):
            hardware.operate("arm")
        hardware.exchange(packet())
        self.assertTrue(hardware.status()["device_fresh"])
        self.assertFalse(hardware.status()["command"]["arm"])
        hardware.operate("arm")
        self.assertTrue(hardware.status()["command"]["arm"])

    def test_sequence_and_reboot_disarm_no_auto_resume(self):
        lab = LabController("hardware")
        lab.exchange(packet())
        lab.operate("arm")
        with self.assertRaises(ValueError):
            lab.exchange(packet())
        self.assertFalse(lab.status()["armed"])
        lab.exchange(packet(1, 1000))
        self.assertFalse(lab.status()["armed"])
        lab.operate("reset_fault")
        ack = lab.status()["command"]["command_id"]
        lab.exchange(packet(2, 2000, command_ack=ack))
        lab.operate("arm")
        lab.exchange(packet(0, 0, boot_id="boot-b"))
        status = lab.status()
        self.assertFalse(status["armed"])
        self.assertEqual(status["fault"], "device_reboot")
        self.assertFalse(status["metrics"]["energy_valid"])

    def test_stale_reading_disarms_and_needs_reset(self):
        with patch("control.time.monotonic", return_value=100):
            lab = LabController("hardware")
            lab.exchange(packet())
            lab.operate("arm")
        with patch("control.time.monotonic", return_value=104):
            self.assertFalse(lab.status()["armed"])
            self.assertFalse(lab.status()["device_fresh"])
            with self.assertRaises(ValueError):
                lab.operate("arm")
            lab.exchange(packet(1, 1000))
            self.assertFalse(lab.status()["armed"])

    def test_energy_uses_measured_samples_not_pwm(self):
        lab = LabController("hardware")
        lab.exchange(packet(power=6))
        lab.exchange(packet(1, 1000, power=12))
        self.assertAlmostEqual(lab.status()["metrics"]["energy_wh"], 9 / 3600)
        lab.exchange(packet(2, 5001, power=12))
        status = lab.status()
        self.assertAlmostEqual(status["metrics"]["energy_wh"], 9 / 3600)
        self.assertFalse(status["metrics"]["energy_valid"])
        self.assertEqual(status["metrics"]["energy_missing_ms"], 4001)

    def test_measured_encoder_completes_relative_job_and_keeps_led(self):
        lab = LabController("hardware")
        lab.exchange(packet(revs=50))
        lab.operate("update_settings", {"job_target_revolutions": 10})
        lab.operate("new_job")
        self.assertFalse(lab.status()["armed"])
        lab.operate("arm")
        cmd = lab.exchange(packet(1, 1000, revs=59, armed=True))
        self.assertFalse(lab.status()["metrics"]["job_completed"])
        self.assertEqual(cmd["target_revolutions"], 60)
        lab.exchange(packet(2, 2000, revs=60, armed=True))
        status = lab.status()
        self.assertTrue(status["metrics"]["job_completed"])
        self.assertEqual(status["command"]["motor_duty"], 0)
        self.assertGreaterEqual(status["command"]["led_duty"], status["settings"]["led_min_duty"])
        energy = status["metrics"]["energy_wh"]
        lab.operate("new_job")
        self.assertEqual(lab.status()["metrics"]["job_revolutions"], 0)
        self.assertEqual(lab.status()["metrics"]["energy_wh"], energy)

    def test_latched_device_fault_and_reset_requires_ack_and_arm(self):
        lab = LabController("hardware")
        lab.exchange(packet(fault="overcurrent"))
        with self.assertRaises(ValueError):
            lab.operate("arm")
        lab.operate("reset_fault")
        with self.assertRaises(ValueError):
            lab.operate("arm")
        cmd_id = lab.status()["command"]["command_id"]
        # The next outgoing packet may still describe the fault before receipt of reset.
        lab.exchange(packet(1, 1000, fault="overcurrent"))
        lab.exchange(packet(2, 2000, command_ack=cmd_id + 1))
        self.assertFalse(lab.status()["armed"])
        lab.operate("arm")
        self.assertTrue(lab.status()["armed"])

    def test_late_completion_remains_a_deadline_miss(self):
        lab = LabController("hardware")
        lab.exchange(packet())
        lab.operate("update_settings", {"job_target_revolutions": 2, "job_deadline_s": 1})
        lab.operate("arm")
        lab.exchange(packet(1, 2000, revs=2, armed=True))
        metrics = lab.status()["metrics"]
        self.assertTrue(metrics["job_completed"])
        self.assertTrue(metrics["deadline_missed"])
        self.assertEqual(metrics["job_completion_s"], 2)

    def test_demo_has_physical_feedback_and_settings_validation(self):
        lab = LabController()
        self.assertFalse(lab.status()["armed"])
        lab.operate("arm")
        with self.assertRaises(ValueError):
            lab.operate("reset_experiment")
        for _ in range(20):
            status = lab.step_demo()
        self.assertGreater(status["metrics"]["energy_wh"], 0)
        self.assertGreater(status["metrics"]["job_revolutions"], 0)
        self.assertGreater(status["telemetry"]["rpm"], 0)
        self.assertNotEqual(status["telemetry"]["rpm"], status["settings"]["motor_target_rpm"])
        for config in ({"budget_wh": float("nan")}, {"led_duty": .1}, {"power_limit_w": True}, {"x": 1}, {"policy": "llm"}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                lab.operate("update_settings", config)
        lab.operate("stop")
        lab.operate("reset_experiment")
        self.assertEqual(lab.status()["metrics"]["energy_wh"], 0)

    def test_invalid_packet_and_device_disarm_stop_server(self):
        lab = LabController("hardware")
        lab.exchange(packet())
        lab.operate("arm")
        arm_id = lab.status()["command"]["command_id"]
        lab.exchange(packet(1, 1000, armed=False, command_ack=arm_id))
        self.assertFalse(lab.status()["armed"])
        self.assertEqual(lab.status()["fault"], "device_disarmed")
        with self.assertRaises(ValueError):
            lab.exchange(packet(2, 2000, power=float("nan")))
        self.assertFalse(lab.status()["command"]["arm"])


if __name__ == "__main__":
    unittest.main()
