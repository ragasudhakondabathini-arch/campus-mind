import os
import socket
import subprocess
import json
import logging
import re
from typing import Dict, Any, Optional

logger = logging.getLogger("HotspotMonitor")

class HotspotMonitor:
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}

    def get_host_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def get_windows_hotspot_ssid_from_reg(self) -> Optional[str]:
        """
        Reads configured Windows Mobile Hotspot SSID directly from registry.
        """
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\icssvc\Settings") as key:
                val, _ = winreg.QueryValueEx(key, "PrivateConnectionSettings")
                if isinstance(val, (bytes, bytearray)):
                    raw = bytes(val[4:])
                    null_pos = raw.find(b'\x00\x00')
                    if null_pos != -1:
                        raw = raw[:null_pos+1]
                    decoded = raw.decode('utf-16le', errors='ignore').rstrip('\x00')
                    if decoded.strip():
                        return decoded.strip()
        except Exception:
            pass
        return None

    def check_windows_hotspot_powershell(self) -> Dict[str, Any]:
        """
        Queries Windows NetworkOperatorTetheringManager and active network interfaces
        using PowerShell to accurately detect Windows Mobile Hotspot state and connected clients.
        """
        ps_cmd = """
        $res = @{
            IsActive = $false
            StatusUnknown = $false
            SSID = $null
            HostIP = $null
            ClientsCount = 0
            Message = "Windows Mobile Hotspot is OFF"
        }

        try {
            # 1. Try WinRT Tethering Manager if accessible
            try {
                Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction SilentlyContinue
                [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType=WindowsRuntime] | Out-Null
                $connProfile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
                if ($connProfile) {
                    $tethering = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($connProfile)
                    if ($tethering) {
                        $opState = $tethering.TetheringOperationalState.ToString()
                        if ($opState -eq "On") {
                            $res.IsActive = $true
                            $res.ClientsCount = $tethering.ClientCount
                            $cfg = $tethering.GetCurrentAccessPointConfiguration()
                            if ($cfg -and $cfg.Ssid) {
                                $res.SSID = $cfg.Ssid
                            }
                            $res.Message = "Windows Mobile Hotspot is ON (Broadcasting)"
                        }
                    }
                }
            } catch {}

            # 2. Inspect active network interfaces
            if (-not $res.IsActive) {
                $interfaces = [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()
                foreach ($iface in $interfaces) {
                    $desc = $iface.Description
                    $status = $iface.OperationalStatus.ToString()
                    $isVirtual = ($desc -like "*Wi-Fi Direct Virtual*" -or $desc -like "*Direct Virtual*" -or $desc -like "*Mobile Hotspot*")

                    if ($isVirtual -and $status -eq "Up") {
                        foreach ($addr in $iface.GetIPProperties().UnicastAddresses) {
                            $ipStr = $addr.Address.ToString()
                            # Hotspot virtual adapter MUST have an assigned non-APIPA private IPv4 (e.g. 192.168.137.1)
                            if ($ipStr -like "192.168.137.*" -or ($ipStr -notlike "169.254.*" -and $ipStr -match '^192\\.168\\.\\d+\\.\\d+$')) {
                                $res.IsActive = $true
                                $res.HostIP = $ipStr
                                $res.Message = "Windows Mobile Hotspot is ON (Broadcasting)"
                                break
                            }
                        }
                    }
                    if ($res.IsActive) { break }
                }
            }

            # 3. Read SSID if active or configured
            if (-not $res.SSID) {
                try {
                    $regKey = Get-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\icssvc\\Settings' -ErrorAction SilentlyContinue
                    if ($regKey -and $regKey.PrivateConnectionSettings) {
                        $bytes = $regKey.PrivateConnectionSettings[4..($regKey.PrivateConnectionSettings.Length - 1)]
                        $enc = [System.Text.Encoding]::Unicode
                        $decoded = $enc.GetString($bytes)
                        $nullIdx = $decoded.IndexOf("`0`0")
                        if ($nullIdx -ge 0) { $decoded = $decoded.Substring(0, $nullIdx) }
                        $res.SSID = $decoded.Trim("`0", " ")
                    }
                } catch {}
            }

            # 4. If active, count clients from ARP table
            if ($res.IsActive) {
                try {
                    $arp = arp -a | Select-String -Pattern '192\\.168\\.137\\.'
                    $count = 0
                    foreach ($line in $arp) {
                        if ($line -notmatch '192\\.168\\.137\\.1\\s' -and $line -notmatch '192\\.168\\.137\\.255') {
                            $count++
                        }
                    }
                    $res.ClientsCount = $count
                } catch {}
            } else {
                $res.ClientsCount = 0
            }

        } catch {
            $res.StatusUnknown = $true
            $res.Message = "Status Unavailable (Detection Failed)"
        }

        $res | ConvertTo-Json -Compress
        """
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=5,
                startupinfo=startupinfo
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return json.loads(proc.stdout.strip())
        except Exception as e:
            logger.debug(f"PowerShell hotspot check failed: {e}")
            
        return {
            "IsActive": False,
            "StatusUnknown": True,
            "SSID": None,
            "HostIP": None,
            "ClientsCount": 0,
            "Message": "Status Unavailable (Detection Failed)"
        }

    def detect_status(self) -> Dict[str, Any]:
        """
        Returns full diagnostic status of the hotspot.
        Reliably detects whether Windows Mobile Hotspot is ON or OFF.
        """
        host_ip = self.get_host_ip()
        reg_ssid = self.get_windows_hotspot_ssid_from_reg()
        ps_res = self.check_windows_hotspot_powershell()
        
        is_active = ps_res.get("IsActive", False)
        status_unknown = ps_res.get("StatusUnknown", False)
        ssid = ps_res.get("SSID") or reg_ssid or "CampusMind-Hotspot"
        clients_count = ps_res.get("ClientsCount", 0) if is_active else 0
        actual_host_ip = ps_res.get("HostIP") or (host_ip if host_ip != "127.0.0.1" else "192.168.137.1")

        if status_unknown:
            diag_msg = "Status Unavailable (Detection Failed)"
        elif is_active:
            diag_msg = f"Windows Mobile Hotspot is ON (Broadcasting '{ssid}')"
        else:
            diag_msg = "Windows Mobile Hotspot is OFF"
            if self.config.get("LOCALHOST_BYPASS", True):
                diag_msg += " (Localhost Bypass Enabled for same-laptop testing)"

        return {
            "hotspot_active": is_active,
            "status_unknown": status_unknown,
            "ssid": ssid,
            "host_ip": actual_host_ip if is_active else host_ip,
            "connected_clients_count": clients_count,
            "diagnostic_message": diag_msg,
            "require_hotspot_on": self.config.get("REQUIRE_HOTSPOT_ON", True),
            "localhost_bypass": self.config.get("LOCALHOST_BYPASS", True),
            "mock_mode": self.config.get("MOCK_MODE", False)
        }
