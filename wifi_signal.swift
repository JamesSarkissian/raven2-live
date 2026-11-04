// Prints the Mac's Wi-Fi signal once a second: "rssi_dBm noise_dBm tx_rate_Mbps".
// Read-only: it only asks macOS about the current connection.
// Built automatically by live_viewer.py into .build/wifi_signal.
import CoreWLAN
import Foundation

setvbuf(stdout, nil, _IOLBF, 0)
while true {
    if let i = CWWiFiClient.shared().interface(), i.rssiValue() != 0 {
        print(i.rssiValue(), i.noiseMeasurement(), i.transmitRate())
    } else {
        print("none")
    }
    sleep(1)
}
