"""In-memory BMC simulator. No sockets, HTTP, firmware execution or device access."""
import threading
from firmware_batch import COMPONENTS, CPLD, INVENTORY, REQUEST_ACTIVE

DEMO_MARKER = b'BMC-DEMO-DO-NOT-FLASH\n'

class DemoClient:
    states = {}
    lock = threading.Lock()
    def __init__(self, address, options):
        self.address, self.options = address, options
        self.allowed_resets = {'GracefulShutdown', 'ForceOff', 'On'}
        self.reset_path = '/redfish/v1/Systems/demo/Actions/ComputerSystem.Reset'
        with self.lock:
            self.state = self.states.setdefault(address, {'power': 'Off', 'versions': dict.fromkeys(COMPONENTS, '0.3'),
                                                        'target': 'Dcu57Cpld', 'ticks': 0, 'expected': '0.5'})
    def login(self, *args):
        pass
    def close(self):
        return ''
    def system(self):
        return {'Model': self.options.model or 'CY62-T58', 'PowerState': self.state['power']}
    def cpld(self):
        ticking = self.state['ticks'] > 0
        if ticking:
            self.state['ticks'] -= 1
            if self.state['ticks'] == 0:
                self.state['versions'][self.state['target']] = self.state['expected']
        return {'Activation': 'xyz.openbmc_project.Software.Activation.Activations.' + ('Activating' if ticking else 'Active'),
                'Progress': 50 if ticking else 100, 'ExtendedVersion': self.state['target'],
                'VendorList': dict.fromkeys(COMPONENTS, 'Lattice'),
                'VersionList': dict(self.state['versions']),
                'EffectiveList': [[c, 'gpio', 'poweroff'] for c in COMPONENTS]}
    def request(self, method, path):
        if method == 'GET' and path.startswith(INVENTORY):
            return {'Version': self.state['versions'][path.removeprefix(INVENTORY)], 'Updateable': True,
                    'Status': {'State': 'Enabled'}}, {}
        raise AssertionError('Unexpected simulator request')
    def write(self, method, path, payload, *, binary=False):
        if binary:
            if not payload.startswith(DEMO_MARKER):
                raise ValueError('演示只接受工具生成的模拟镜像')
            self.state['expected'] = payload.decode().splitlines()[1]
        elif path.endswith('/ExtendedVersion'):
            self.state['target'] = payload['data']
        elif path.endswith('/RequestedActivation'):
            assert payload['data'] == REQUEST_ACTIVE
            self.state['ticks'] = 2
        elif path == self.reset_path:
            self.state['power'] = 'On' if payload['ResetType'] == 'On' else 'Off'
        else:
            raise AssertionError('Unexpected simulator write')
