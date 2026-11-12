import subprocess
import re
import logging
from typing import List, Dict, Any

logger = logging.getLogger("NetworkScanner")

class NetworkScanner:
    def __init__(self):
        pass

    def scan_arp_table(self) -> List[Dict[str, str]]:
        """
        Scans local ARP table on Windows to get list of active IP & MAC addresses.
        """
        devices = []
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            proc = subprocess.run(
                ["arp", "-a"],
                capture_output=True,
                text=True,
                timeout=3,
                startupinfo=startupinfo
            )
            if proc.returncode == 0:
                lines = proc.stdout.splitlines()
                # Line example:  192.168.137.24   b4-b5-2f-xx-xx-xx     dynamic
                pattern = re.compile(r'^\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\s+([0-9a-fA-F-]+)\s+(\w+)')
                for line in lines:
                    match = pattern.match(line)
                    if match:
                        ip, mac, entry_type = match.groups()
                        # Filter out broadcast/multicast
                        if not ip.startswith("224.") and not ip.startswith("239.") and not ip.endswith(".255") and mac != "ff-ff-ff-ff-ff-ff":
                            devices.append({
                                "ip": ip,
                                "mac": mac.replace("-", ":").upper(),
                                "type": entry_type
                            })
        except Exception as e:
            logger.debug(f"ARP scan error: {e}")

        return devices

    def is_ip_reachable(self, ip: str) -> bool:
        """
        Quick check if an IP is currently reachable.
        """
        if ip in ("127.0.0.1", "localhost", "::1"):
            return True
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            res = subprocess.run(
                ["ping", "-n", "1", "-w", "500", ip],
                capture_output=True,
                startupinfo=startupinfo
            )
            return res.returncode == 0
        except Exception:
            return False

