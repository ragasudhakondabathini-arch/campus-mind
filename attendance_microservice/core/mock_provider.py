import random
import time
from typing import List, Dict, Any

# Internal pool of student names as specified by blueprint
INTERNAL_NAMES_POOL = [
    "Rahul Verma", "Priya Nair", "Arjun Mehta", "Sneha Reddy", "Karan Gupta",
    "Ananya Desai", "Rohan Iyer", "Meera Joshi", "Vikram Singh", "Pooja Patel",
    "Aditya Rao", "Divya Sharma", "Siddharth Jain", "Neha Kulkarni", "Varun Kapoor",
    "Ishita Sen", "Tanmay Bhat", "Ritu Saxena", "Manish Pandey", "Kavita Pillai",
    "Aakash Banerjee", "Deepika Choudhury", "Nikhil Agarwal", "Shweta Tiwari",
    "Gaurav Nambiar", "Swati Bhattacharya", "Harsh Vardhan", "Preeti Menon",
    "Abhishek Mishra", "Sunita Biswas", "Rajesh Khanna", "Pallavi Joshi",
    "Amitabh Saxena", "Anjali Deshmukh", "Sanjay Singhania"
]

class MockProvider:
    """
    Simulates realistic student joins, heartbeats, disconnects, and reconnects
    for full end-to-end testing without real devices or physical hotspot.
    """
    def __init__(self, count_min: int = 15, count_max: int = 25, force_scenario: str = None):
        # 1. Generate 15–25 fake students
        available_names = list(INTERNAL_NAMES_POOL)
        random.shuffle(available_names)
        self.count = random.randint(count_min, min(count_max, len(available_names)))
        
        self.selected_students = []
        for i in range(1, self.count + 1):
            # Format student ID
            sid = f"STU{i:03d}"
            name = available_names[i - 1]
            
            # Fake IP in 192.168.137.x range (hotspot subnet)
            ip = f"192.168.137.{100 + i}"
            
            # Fake MAC address in specified AA:BB:CC:XX:XX:XX pattern
            mac = f"AA:BB:CC:{random.randint(0x10, 0xFE):02X}:{random.randint(0x10, 0xFE):02X}:{i:02X}"
            
            self.selected_students.append({
                "student_id": sid,
                "name": name,
                "ip": ip,
                "mac": mac
            })

        # 2. Schedule join times spread across session
        # Initial wave (first ~70% of students) joins quickly (1-20s) for live visual feedback
        # Remaining wave joins staggered over subsequent intervals
        self.student_states = {}
        for idx, s in enumerate(self.selected_students):
            sid = s["student_id"]
            if idx < int(self.count * 0.7):
                join_target = random.randint(1, 20)
            else:
                join_target = random.randint(25, 90)

            self.student_states[sid] = {
                "joined": False,
                "join_target_sec": join_target,
                "is_connected": True,
                "will_disconnect": False,
                "disconnect_at_sec": 0,
                "reconnect_at_sec": 0,
                "has_disconnected": False,
                "has_reconnected": False,
                "disconnect_status": "PAUSED",
                "reconnect_status": "RECONNECTED"
            }

        # 3. Session Scenario:
        # Some sessions must randomly have ALL students PRESENT without any disconnections (e.g. 35% probability).
        if force_scenario == "all_present":
            self.is_all_present_session = True
        elif force_scenario == "mixed":
            self.is_all_present_session = False
        else:
            self.is_all_present_session = (random.random() < 0.35)

        if not self.is_all_present_session:
            # 10%–20% of students disconnect once and reconnect or drop
            num_disconnect = max(2, int(self.count * random.uniform(0.12, 0.22)))
            early_students = [s for s in self.selected_students if self.student_states[s["student_id"]]["join_target_sec"] <= 20]
            if not early_students:
                early_students = self.selected_students[:num_disconnect]
            disconnect_candidates = random.sample(early_students, min(num_disconnect, len(early_students)))
            
            for idx, cand in enumerate(disconnect_candidates):
                csid = cand["student_id"]
                cstate = self.student_states[csid]
                cstate["will_disconnect"] = True

                if idx == 0:
                    # Candidate 1: Simulates DROPPED (< 25% attendance)
                    # Disconnects early and does not reconnect
                    cstate["disconnect_at_sec"] = cstate["join_target_sec"] + random.randint(10, 18)
                    cstate["reconnect_at_sec"] = 999999
                    cstate["disconnect_status"] = "DROPPED"
                    cstate["reconnect_status"] = "JOINED"
                elif idx == 1:
                    # Candidate 2: Simulates PARTIAL (25%–74% attendance)
                    # Disconnects with PAUSED, stays disconnected for significant gap, then reconnects
                    cstate["disconnect_at_sec"] = cstate["join_target_sec"] + random.randint(12, 22)
                    gap = random.randint(30, 45)
                    cstate["reconnect_at_sec"] = cstate["disconnect_at_sec"] + gap
                    cstate["disconnect_status"] = "PAUSED"
                    cstate["reconnect_status"] = "RECONNECTED"
                else:
                    # Candidate 3+: Brief temporary blip (maintains ≥ 75% -> PRESENT)
                    cstate["disconnect_at_sec"] = cstate["join_target_sec"] + random.randint(15, 25)
                    gap = random.randint(8, 15)
                    cstate["reconnect_at_sec"] = cstate["disconnect_at_sec"] + gap
                    cstate["disconnect_status"] = "PAUSED"
                    cstate["reconnect_status"] = "RECONNECTED"
        else:
            # All-Present Session: 0 disconnections, all students achieve 100% PRESENT
            pass

    def get_initial_batch(self) -> List[Dict[str, Any]]:
        return self.selected_students

    def simulate_tick(self, elapsed_sec: float) -> List[Dict[str, Any]]:
        """
        Evaluates current session elapsed time and emits simulated actions:
        - join: Student joins session
        - heartbeat: Regular periodic heartbeat to maintain presence
        - disconnect: Student stops sending heartbeats (disconnect event with PAUSED or DROPPED status)
        - reconnect: Student resumes sending heartbeats after gap (with RECONNECTED or JOINED status)
        """
        actions = []
        for s in self.selected_students:
            sid = s["student_id"]
            state = self.student_states[sid]

            # 1. Check if student should join
            if not state["joined"]:
                if elapsed_sec >= state["join_target_sec"]:
                    state["joined"] = True
                    actions.append({"action": "join", "student": s})
                continue

            # 2. Check scheduled disconnect event (10–20% of students)
            if state["will_disconnect"]:
                if not state["has_disconnected"] and elapsed_sec >= state["disconnect_at_sec"] and elapsed_sec < state["reconnect_at_sec"]:
                    state["has_disconnected"] = True
                    state["is_connected"] = False
                    actions.append({
                        "action": "disconnect",
                        "student_id": sid,
                        "status": state.get("disconnect_status", "PAUSED")
                    })
                    continue

                if state["has_disconnected"] and not state["has_reconnected"] and elapsed_sec >= state["reconnect_at_sec"]:
                    state["has_reconnected"] = True
                    state["is_connected"] = True
                    actions.append({
                        "action": "reconnect",
                        "student_id": sid,
                        "status": state.get("reconnect_status", "RECONNECTED"),
                        "student": s
                    })
                    continue

            # 3. Connected student sends heartbeat
            if state["is_connected"]:
                actions.append({"action": "heartbeat", "student_id": sid})

        return actions

    def get_final_attendance(self, planned_duration_minutes: int) -> Dict[str, int]:
        """
        Calculates realistic final attended seconds for mock students upon session finalization:
        - PRESENT students: attended 78%–96% of class duration (>= 75%)
        - PARTIAL students: attended 35%–65% of class duration (25%–74%)
        - DROPPED students: attended 5%–18% of class duration (< 25%)
        """
        planned_sec = max(planned_duration_minutes * 60, 60)
        result = {}
        for s in self.selected_students:
            sid = s["student_id"]
            st = self.student_states.get(sid, {})
            if self.is_all_present_session or not st.get("will_disconnect", False):
                pct = random.uniform(0.78, 0.96)
            elif st.get("disconnect_status") == "DROPPED":
                pct = random.uniform(0.05, 0.18)
            elif st.get("disconnect_status") == "PAUSED":
                pct = random.uniform(0.35, 0.65)
            else:
                pct = random.uniform(0.78, 0.92)

            result[sid] = max(1, int(planned_sec * pct))
        return result
