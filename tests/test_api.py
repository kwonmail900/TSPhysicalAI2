import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import LabServer


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servers = []
        for mode in ('demo', 'hardware'):
            server = LabServer(('127.0.0.1', 0), mode, 'test-device-token-only')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cls.servers.append((server, thread))
        cls.demo, cls.hardware = [f'http://127.0.0.1:{s.server_port}' for s, _ in cls.servers]

    @classmethod
    def tearDownClass(cls):
        for server, thread in cls.servers:
            server.shutdown()
            server.server_close()
            thread.join()

    def get(self, base, path):
        with urlopen(base + path, timeout=10) as response:
            return response.read()

    def post(self, base, path, payload, **headers):
        req = Request(base + path, json.dumps(payload).encode(), headers={'Content-Type':'application/json', **headers})
        with urlopen(req, timeout=10) as response:
            return json.load(response)

    def test_assets_and_mode_provenance(self):
        for path in ('/', '/app.js', '/style.css'):
            self.assertTrue(self.get(self.demo, path))
        demo = json.loads(self.get(self.demo, '/api/status'))
        hardware = json.loads(self.get(self.hardware, '/api/status'))
        self.assertTrue(demo['capabilities']['synthetic'])
        self.assertFalse(hardware['capabilities']['synthetic'])
        self.assertIsNone(hardware['telemetry'])
        self.assertFalse(hardware['armed'])

    def test_demo_arm_feedback_and_emergency_stop(self):
        server = self.servers[0][0]
        self.post(self.demo, '/api/action', {'action':'reset_fault'})
        server.lab.step_demo()
        self.post(self.demo, '/api/action', {'action':'new_job'})
        result = self.post(self.demo, '/api/action', {'action':'arm'})
        self.assertTrue(result['armed'])
        for _ in range(10):
            server.lab.step_demo()
        result = json.loads(self.get(self.demo, '/api/status'))
        self.assertGreater(result['metrics']['energy_wh'], 0)
        self.assertGreater(result['metrics']['job_revolutions'], 0)
        result = self.post(self.demo, '/api/action', {'action':'emergency_stop'})
        self.assertFalse(result['armed'])
        self.assertTrue(result['command']['emergency_stop'])
        self.assertEqual(result['command']['motor_duty'], 0)
        self.assertIn('power_w', self.get(self.demo, '/api/log.csv').decode('utf-8-sig'))
        self.assertIn('metrics', json.loads(self.get(self.demo, '/api/events.json')))

    def test_forecast_requires_explicit_budget_apply(self):
        initial = json.loads(self.get(self.demo, '/api/status'))['settings']['budget_wh']
        result = self.post(self.demo, '/api/forecast', {'scale_wh_per_kwh':.003})
        self.assertEqual(result['status']['settings']['budget_wh'], initial)
        recommended = result['forecast']['budget_wh']
        applied = self.post(self.demo, '/api/action', {'action':'update_settings','payload':{'budget_wh':recommended}})
        self.assertEqual(applied['settings']['budget_wh'], recommended)
        self.assertIn('date,energy_kwh', self.get(self.demo, '/api/forecast-template.csv').decode('utf-8-sig'))

    def test_hardware_authenticated_exchange_and_order(self):
        packet = {'device_id':'node-1', 'boot_id':'test-boot', 'seq':1, 'uptime_ms':1000,
                  'voltage_v':12, 'current_a':0, 'power_w':0, 'rpm':0,
                  'total_revolutions':0, 'fault':'', 'armed':False, 'command_ack':0}
        with self.assertRaises(HTTPError) as error:
            self.post(self.hardware, '/api/device/exchange', packet)
        self.assertEqual(error.exception.code, 401)
        command = self.post(self.hardware, '/api/device/exchange', packet, **{'X-Device-Token':'test-device-token-only'})
        self.assertFalse(command['arm'])
        self.assertGreater(command['command_id'], 2**32)
        self.assertEqual(command['ttl_ms'], 1500)
        result = self.post(self.hardware, '/api/action', {'action':'arm'})
        self.assertTrue(result['command']['arm'])
        packet.update(seq=2, uptime_ms=1500, armed=True, command_ack=result['command']['command_id'], power_w=6, current_a=.5, rpm=60,total_revolutions=.5)
        command = self.post(self.hardware, '/api/device/exchange', packet, **{'X-Device-Token':'test-device-token-only'})
        self.assertTrue(command['arm'])
        with self.assertRaises(HTTPError) as error:
            self.post(self.hardware, '/api/device/exchange', packet, **{'X-Device-Token':'test-device-token-only'})
        self.assertEqual(error.exception.code, 400)
        self.assertFalse(json.loads(self.get(self.hardware, '/api/status'))['armed'])

    def test_invalid_operations(self):
        with self.assertRaises(HTTPError) as error:
            self.post(self.demo, '/api/action', {'action':'update_settings','payload':{'led_duty':-1}})
        self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.post(self.demo, '/api/action', {'action':'arm'}, Origin='https://example.com')
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError) as error:
            self.get(self.demo, '/firmware/esp32_node/config.h')
        self.assertEqual(error.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
