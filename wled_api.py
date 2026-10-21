"""
WLED API communication module
Handles sending JSON commands to WLED via HTTP
"""
import requests
import logging
import time
from typing import Optional, Tuple


class WLEDController:
    def __init__(self, ip_address: str):
        self.ip_address = ip_address
        self.last_request_time = 0
        self.min_request_interval = 0.05  # 50ms minimum between requests
        
    def _get_url(self):
        """Build API URL, handle both IP and mDNS"""
        ip = self.ip_address.strip()
        if not ip.startswith('http'):
            ip = f'http://{ip}'
        return f'{ip}/json/state'
    
    def _rate_limit(self):
        """Enforce minimum time between requests"""
        now = time.time()
        elapsed = now - self.last_request_time
        if elapsed < self.min_request_interval:
            time.sleep(self.min_request_interval - elapsed)
        self.last_request_time = time.time()
    
    def set_color_and_brightness(
        self,
        r: int,
        g: int,
        b: int,
        brightness: int,
        effect_id: int = 0,
        effect_speed: int = 128
    ) -> Tuple[bool, Optional[str]]:
        """
        Send color and brightness to WLED
        
        Args:
            r, g, b: RGB values (0-255)
            brightness: Overall brightness (0-255)
            effect_id: WLED effect ID (0 = solid color, 1+ = effects)
            effect_speed: Effect speed parameter (0-255)
        
        Returns:
            (success: bool, error_message: Optional[str])
        """
        self._rate_limit()
        
        payload = {
            "on": True,
            "bri": brightness,
            "seg": [{
                "col": [[r, g, b]],
                "fx": effect_id,
                "sx": effect_speed
            }]
        }
        
        try:
            response = requests.post(
                self._get_url(),
                json=payload,
                timeout=2
            )
            
            if response.status_code == 200:
                return True, None
            else:
                return False, f"HTTP {response.status_code}"
                
        except requests.exceptions.Timeout:
            return False, "Request timeout"
        except requests.exceptions.ConnectionError:
            return False, "Connection failed - check IP/network"
        except Exception as e:
            return False, str(e)
    
    def turn_off(self) -> Tuple[bool, Optional[str]]:
        """Turn off LEDs"""
        self._rate_limit()
        
        payload = {"on": False}
        
        try:
            response = requests.post(
                self._get_url(),
                json=payload,
                timeout=2
            )
            return response.status_code == 200, None
        except Exception as e:
            return False, str(e)
    
    def test_connection(self) -> Tuple[bool, str]:
        """
        Test connection to WLED device
        Returns (success, message)
        """
        try:
            response = requests.get(
                self._get_url(),
                timeout=3
            )
            if response.status_code == 200:
                return True, "Connected successfully"
            else:
                return False, f"HTTP {response.status_code}"
        except requests.exceptions.Timeout:
            return False, "Timeout - device not responding"
        except requests.exceptions.ConnectionError:
            return False, "Connection failed - check IP address and network"
        except Exception as e:
            return False, str(e)
