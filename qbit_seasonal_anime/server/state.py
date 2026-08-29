from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, Any, List, Optional
import asyncio

class ServerState:
    def __init__(self):
        self.logs: Deque[Dict[str, Any]] = deque(maxlen=500)
        self.wake_event: asyncio.Event = asyncio.Event()
        self.is_running_cycle: bool = False
        self.last_cycle_time: Optional[datetime] = None
        self.last_cycle_logs: List[str] = []
        self.next_check_reason: str = "Initializing supervisor..."
        self.next_check_seconds: int = 0
        self.next_check_time: Optional[datetime] = None
        self.target_next_check_time: Optional[datetime] = None

    def add_log(self, message: str, level: str = "INFO"):
        now = datetime.now(timezone.utc)
        self.logs.append({
            "timestamp": now.isoformat(),
            "time_str": now.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        })

    def trigger_immediate_cycle(self):
        self.wake_event.set()


state = ServerState()
