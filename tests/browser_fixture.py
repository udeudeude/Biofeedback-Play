# Synthetic hardware fixture for browser tests. Never connects to physical sensors.
import sys,time,math,threading,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import biofeedback_play as app
app.RECORDINGS=Path(tempfile.mkdtemp(prefix='biofeedback-qa-'))
app.SETTINGS_PATH=app.RECORDINGS/'settings.json'
app.list_muse_serial_ports=lambda:[]
app.list_muse_bluetooth_devices=lambda:[]
with patch('threading.Thread.start'):
    app.STATE=app.BiofeedbackState()
state=app.STATE
state.muse_port='bt://00:06:66:78:A6:20';state.muse_version='MUSE APP FW-7.8.0';state.muse_battery={'percentage':3};state.muse_battery_received_monotonic=time.monotonic()
fixture_devices=[dict(path_token='lightstone',known='Lightstone',product='Lightstone',manufacturer='Wild Divine',vendor_id=app.VENDOR_ID,product_id=app.PRODUCT_ID,vendor_hex='0x14FA',product_hex='0x0001',usage_page=1,usage=0,obviously_unrelated=False),dict(path_token='emwave',known='emWave',product='emWave',manufacturer='HeartMath',vendor_id=app.EMWAVE_VENDOR_ID,product_id=app.EMWAVE_PRODUCT_ID,vendor_hex='0x0E30',product_hex='0x0002',usage_page=1,usage=0,obviously_unrelated=False),dict(path_token='unknown',known='',product='Unknown input with a very long device name',manufacturer='Unknown',vendor_id=555,product_id=666,vendor_hex='0x022B',product_hex='0x029A',usage_page=1,usage=0,obviously_unrelated=False)]
app.hid_device_list=lambda:fixture_devices
app.test_hid_device=lambda token:{'opened':True,'device':fixture_devices[0]}
def capture(token,seconds):
    time.sleep(seconds)
    return {'summary':'Captured test reports','saved_path':'/tmp/example.json','report_count':20}
app.capture_hid_device=capture

def pump():
    while not state.shutdown:
        t=time.monotonic()-state.started_monotonic
        state._store_sample(1000+int(20*math.sin(t)),500+int(50*math.sin(t*7)))
        state.connected=True
        state._store_emwave_packet({'counter':int(t*50)%256,'samples':[120+int(30*math.sin(t*7+i*.1)) for i in range(6)],'gap':0},1)
        state.emwave_connected=True
        state.muse_connected=True;state.muse_last_data_monotonic=time.monotonic();state.muse_last_eeg_monotonic=time.monotonic();state.muse_last_accel_monotonic=time.monotonic()
        for j in range(25):
            state._store_muse_eeg({'microvolts':tuple(1000+20*math.sin((t+j/500)*62.83+i) for i in range(4))})
        state._store_muse_accel((12,24,-5))
        state.muse_contact_quality={c:{'level':'poor' if c=='tp9' else 'good','spread_uv':120 if c=='tp9' else 30} for c in ['tp9','fp1','fp2','tp10']}
        for id,definition in app.SIGNAL_DEFINITIONS.items():
            if definition.get('derived') and not id.startswith('camera.'):
                state._store_derived(id,20+10*math.sin(t/2)+len(id),t)
        time.sleep(.1)
threading.Thread(target=pump,daemon=True).start()
server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
app.SERVER=server
print(server.server_address[1],flush=True)
server.serve_forever()
