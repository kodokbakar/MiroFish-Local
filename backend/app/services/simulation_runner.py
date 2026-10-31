"""
OASIS Simulation Runner
Run simulations in the background and record actions for each Agent, supporting real-time status monitoring
"""

import os
import sys
import json
import time
import asyncio
import threading
import subprocess
import signal
import atexit
from typing import Dict, Any, List, Optional, Union, TYPE_CHECKING
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from queue import Queue

if TYPE_CHECKING:
    from ..storage.graph_storage import GraphStorage

from ..config import Config
from ..utils.logger import get_logger
from .graph_memory_updater import GraphMemoryManager
from .simulation_ipc import SimulationIPCClient, CommandType, IPCResponse

logger = get_logger('mirofish.simulation_runner')

# Flag whether cleanup function is registered
_cleanup_registered = False

# Platform detection
IS_WINDOWS = sys.platform == 'win32'


class RunnerStatus(str, Enum):
    """Runner status"""
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AgentAction:
    """Agent action record"""
    round_num: int
    timestamp: str
    platform: str  # twitter / reddit
    agent_id: int
    agent_name: str
    action_type: str  # CREATE_POST, LIKE_POST, etc.
    action_args: Dict[str, Any] = field(default_factory=dict)
    result: Optional[str] = None
    success: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "round_num": self.round_num,
            "timestamp": self.timestamp,
            "platform": self.platform,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "action_type": self.action_type,
            "action_args": self.action_args,
            "result": self.result,
            "success": self.success,
        }


@dataclass
class RoundSummary:
    """Round summary"""
    round_num: int
    start_time: str
    end_time: Optional[str] = None
    simulated_hour: int = 0
    twitter_actions: int = 0
    reddit_actions: int = 0
    active_agents: List[int] = field(default_factory=list)
    actions: List[AgentAction] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "round_num": self.round_num,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "simulated_hour": self.simulated_hour,
            "twitter_actions": self.twitter_actions,
            "reddit_actions": self.reddit_actions,
            "active_agents": self.active_agents,
            "actions_count": len(self.actions),
            "actions": [a.to_dict() for a in self.actions],
        }


@dataclass
class SimulationRunState:
    """Simulation run state (real-time)"""
    simulation_id: str
    runner_status: RunnerStatus = RunnerStatus.IDLE

    # Progress information
    current_round: int = 0
    total_rounds: int = 0
    simulated_hours: int = 0
    total_simulation_hours: int = 0

    # Per-platform independent rounds and simulated time (for dual-platform parallel display)
    twitter_current_round: int = 0
    reddit_current_round: int = 0
    twitter_simulated_hours: int = 0
    reddit_simulated_hours: int = 0

    # Platform status
    twitter_running: bool = False
    reddit_running: bool = False
    twitter_actions_count: int = 0
    reddit_actions_count: int = 0

    # Platform completion status (detected via simulation_end events in actions.jsonl)
    twitter_completed: bool = False
    reddit_completed: bool = False

    # Round summary
    rounds: List[RoundSummary] = field(default_factory=list)

    # Actions list
    actions: List[AgentAction] = field(default_factory=list)

    # Recent actions (for frontend real-time display)
    recent_actions: List[AgentAction] = field(default_factory=list)
    max_recent_actions: int = 50

    # Timestamps
    started_at: Optional[str] = None
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None

    # Error message
    error: Optional[str] = None

    # Process ID (for stopping)
    process_pid: Optional[int] = None

    def add_action(self, action: AgentAction):
        """Add action to recent actions list"""
        self.recent_actions.insert(0, action)
        if len(self.recent_actions) > self.max_recent_actions:
            self.recent_actions = self.recent_actions[:self.max_recent_actions]
        if hasattr(self, "actions") and isinstance(self.actions, list):
            self.actions.append(action)

        if action.platform == "twitter":
            self.twitter_actions_count += 1
        else:
            self.reddit_actions_count += 1
        
        self.updated_at = datetime.now().isoformat()
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "simulation_id": self.simulation_id,
            "runner_status": self.runner_status.value,
            "current_round": self.current_round,
            "total_rounds": self.total_rounds,
            "simulated_hours": self.simulated_hours,
            "total_simulation_hours": self.total_simulation_hours,
            "progress_percent": round(self.current_round / max(self.total_rounds, 1) * 100, 1),
            # Per-platform independent rounds and time
            "twitter_current_round": self.twitter_current_round,
            "reddit_current_round": self.reddit_current_round,
            "twitter_simulated_hours": self.twitter_simulated_hours,
            "reddit_simulated_hours": self.reddit_simulated_hours,
            "twitter_running": self.twitter_running,
            "reddit_running": self.reddit_running,
            "twitter_completed": self.twitter_completed,
            "reddit_completed": self.reddit_completed,
            "twitter_actions_count": self.twitter_actions_count,
            "reddit_actions_count": self.reddit_actions_count,
            "total_actions_count": self.twitter_actions_count + self.reddit_actions_count,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "process_pid": self.process_pid,
        }

    def to_detail_dict(self) -> Dict[str, Any]:
        """Details with recent actions"""
        result = self.to_dict()
        result["recent_actions"] = [a.to_dict() for a in self.recent_actions]
        result["rounds_count"] = len(self.rounds)
        return result


class SimulationRunner:
    """
    Simulation Runner
    
    Responsible for:
    1. Running OASIS simulations in background processes
    2. Parsing run logs and recording actions for each Agent
    3. Providing real-time status query interfaces
    4. Supporting pause/stop/resume operations
    """
    
    # Storage directory for run state
    RUN_STATE_DIR = os.path.join(
        os.path.dirname(__file__),
        '../../uploads/simulations'
    )
    
    # Script directory
    SCRIPTS_DIR = os.path.join(
        os.path.dirname(__file__),
        '../../scripts'
    )
    
    # In-memory run state
    _run_states: Dict[str, SimulationRunState] = {}
    _processes: Dict[str, subprocess.Popen] = {}
    _action_queues: Dict[str, Queue] = {}
    _monitor_threads: Dict[str, threading.Thread] = {}
    _stdout_files: Dict[str, Any] = {}  # Store stdout file handles
    _stderr_files: Dict[str, Any] = {}  # Store stderr file handles

    # In-memory action cache per simulation and thread-safe lock
    _action_cache: Dict[str, List[AgentAction]] = {}
    _action_cache_lock: threading.Lock = threading.Lock()

    # In-memory last saved state for dirty checking and lock for atomic save
    _last_saved_state: Dict[str, Dict[str, Any]] = {}
    _state_save_lock: threading.Lock = threading.Lock()
    
    # Graph memory update configuration
    _graph_memory_enabled: Dict[str, bool] = {}  # simulation_id -> enabled
    @classmethod
    def is_graph_memory_enabled(cls, simulation_id: str) -> bool:
        """Check if graph memory update is enabled for a simulation"""
        return cls._graph_memory_enabled.get(simulation_id, False)

    
    @classmethod
    def get_run_state(cls, simulation_id: str) -> Optional[SimulationRunState]:
        """Get run state"""
        if simulation_id in cls._run_states:
            return cls._run_states[simulation_id]
        
        # Try to load from file
        state = cls._load_run_state(simulation_id)
        if state:
            cls._run_states[simulation_id] = state
        return state
    
    @classmethod
    def _load_run_state(cls, simulation_id: str) -> Optional[SimulationRunState]:
        """Load run state from file"""
        state_file = os.path.join(cls.RUN_STATE_DIR, simulation_id, "run_state.json")
        if not os.path.exists(state_file):
            return None
        
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            state = SimulationRunState(
                simulation_id=simulation_id,
                runner_status=RunnerStatus(data.get("runner_status", "idle")),
                current_round=data.get("current_round", 0),
                total_rounds=data.get("total_rounds", 0),
                simulated_hours=data.get("simulated_hours", 0),
                total_simulation_hours=data.get("total_simulation_hours", 0),
                # Per-platform independent rounds and time
                twitter_current_round=data.get("twitter_current_round", 0),
                reddit_current_round=data.get("reddit_current_round", 0),
                twitter_simulated_hours=data.get("twitter_simulated_hours", 0),
                reddit_simulated_hours=data.get("reddit_simulated_hours", 0),
                twitter_running=data.get("twitter_running", False),
                reddit_running=data.get("reddit_running", False),
                twitter_completed=data.get("twitter_completed", False),
                reddit_completed=data.get("reddit_completed", False),
                twitter_actions_count=data.get("twitter_actions_count", 0),
                reddit_actions_count=data.get("reddit_actions_count", 0),
                started_at=data.get("started_at"),
                updated_at=data.get("updated_at", datetime.now().isoformat()),
                completed_at=data.get("completed_at"),
                error=data.get("error"),
                process_pid=data.get("process_pid"),
            )

            # Load recent actions
            actions_data = data.get("recent_actions", [])
            for a in actions_data:
                state.recent_actions.append(AgentAction(
                    round_num=a.get("round_num", 0),
                    timestamp=a.get("timestamp", ""),
                    platform=a.get("platform", ""),
                    agent_id=a.get("agent_id", 0),
                    agent_name=a.get("agent_name", ""),
                    action_type=a.get("action_type", ""),
                    action_args=a.get("action_args", {}),
                    result=a.get("result"),
                    success=a.get("success", True),
                ))
            
            return state
        except Exception as e:
            logger.error(f"Failed to load run state: {str(e)}")
            return None
    
    @classmethod
    def _save_run_state(cls, state: SimulationRunState):
        """Save run state to file with dirty check and atomic file replacement"""
        sim_dir = os.path.join(cls.RUN_STATE_DIR, state.simulation_id)
        os.makedirs(sim_dir, exist_ok=True)
        state_file = os.path.join(sim_dir, "run_state.json")

        data = state.to_detail_dict()

        with cls._state_save_lock:
            last_saved = cls._last_saved_state.get(state.simulation_id)
            if last_saved is not None and last_saved == data and os.path.exists(state_file):
                cls._run_states[state.simulation_id] = state
                return

            tmp_file = os.path.join(sim_dir, f"run_state.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                with open(tmp_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_file, state_file)
                cls._last_saved_state[state.simulation_id] = data
            except Exception:
                if os.path.exists(tmp_file):
                    try:
                        os.remove(tmp_file)
                    except OSError:
                        pass
                raise

        cls._run_states[state.simulation_id] = state
    
    @classmethod
    def start_simulation(
        cls,
        simulation_id: str,
        platform: str = "parallel",  # twitter / reddit / parallel
        max_rounds: int = None,  # Maximum simulation rounds (optional, for truncating long simulations)
        enable_graph_memory_update: bool = False,  # Whether to update activities to the graph
        graph_id: str = None,  # Graph ID (required when enabling graph updates)
        storage: Optional["GraphStorage"] = None  # GraphStorage instance (required if enable_graph_memory_update)
    ) -> SimulationRunState:
        """
        Start simulation

        Args:
            simulation_id: Simulation ID
            platform: Platform to run (twitter/reddit/parallel)
            max_rounds: Maximum simulation rounds (optional, for truncating long simulations)
            enable_graph_memory_update: Whether to dynamically update Agent activities to the graph
            graph_id: Graph ID (required when enabling graph updates)

        Returns:
            SimulationRunState
        """
        # Check if already running
        existing = cls.get_run_state(simulation_id)
        is_alive = cls.is_process_alive(simulation_id, existing)
        if existing and existing.runner_status in [RunnerStatus.RUNNING, RunnerStatus.STARTING] and is_alive:
            raise ValueError(f"Simulation already running: {simulation_id}")
        elif is_alive:
            logger.warning(f"Terminating lingering process for simulation {simulation_id} before starting")
            try:
                cls.stop_simulation(simulation_id)
            except Exception as e:
                logger.warning(f"Error stopping lingering simulation {simulation_id}: {e}")
        # Wait for any previous monitor thread to exit before starting anew
        existing_thread = cls._monitor_threads.get(simulation_id)
        if existing_thread and existing_thread.is_alive() and existing_thread != threading.current_thread():
            logger.info(f"Waiting for previous monitor thread to exit: {simulation_id}")
            existing_thread.join(timeout=5.0)
            if not existing_thread.is_alive() and cls._monitor_threads.get(simulation_id) is existing_thread:
                cls._monitor_threads.pop(simulation_id, None)
        # Load simulation config
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        config_path = os.path.join(sim_dir, "simulation_config.json")
        
        if not os.path.exists(config_path):
            raise ValueError(f"Simulation config does not exist, call /prepare endpoint first")
        
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # Initialize run state
        time_config = config.get("time_config", {})
        total_hours = time_config.get("total_simulation_hours", 72)
        minutes_per_round = time_config.get("minutes_per_round", 30)
        total_rounds = int(total_hours * 60 / minutes_per_round)
        
        # If max_rounds specified, truncate
        if max_rounds is not None and max_rounds > 0:
            original_rounds = total_rounds
            total_rounds = min(total_rounds, max_rounds)
            if total_rounds < original_rounds:
                logger.info(f"Rounds truncated: {original_rounds} -> {total_rounds} (max_rounds={max_rounds})")
        
        state = SimulationRunState(
            simulation_id=simulation_id,
            runner_status=RunnerStatus.STARTING,
            total_rounds=total_rounds,
            total_simulation_hours=total_hours,
            started_at=datetime.now().isoformat(),
        )

        with cls._action_cache_lock:
            cls._action_cache[simulation_id] = []
        with cls._state_save_lock:
            cls._last_saved_state.pop(simulation_id, None)

        cls._save_run_state(state)
        
        # If graph memory update enabled, create updater
        if enable_graph_memory_update:
            if not graph_id:
                raise ValueError("Must provide graph_id when enabling graph memory update")
            
            if not storage:
                raise ValueError("Must provide storage (GraphStorage) when enabling graph memory update")
            
            try:
                GraphMemoryManager.create_updater(simulation_id, graph_id, storage)
                cls._graph_memory_enabled[simulation_id] = True
                logger.info(f"Graph memory update enabled: simulation_id={simulation_id}, graph_id={graph_id}")
            except Exception as e:
                logger.error(f"Failed to create graph memory updater: {e}")
                cls._graph_memory_enabled[simulation_id] = False
        else:
            cls._graph_memory_enabled[simulation_id] = False
        
        # Determine which script to run (scripts located in backend/scripts/ directory)
        if platform == "twitter":
            script_name = "run_twitter_simulation.py"
            state.twitter_running = True
        elif platform == "reddit":
            script_name = "run_reddit_simulation.py"
            state.reddit_running = True
        else:
            script_name = "run_parallel_simulation.py"
            state.twitter_running = True
            state.reddit_running = True
        
        script_path = os.path.join(cls.SCRIPTS_DIR, script_name)
        
        if not os.path.exists(script_path):
            raise ValueError(f"Script does not exist: {script_path}")
        
        # Create action queue
        action_queue = Queue()
        cls._action_queues[simulation_id] = action_queue
        
        # Start simulation process
        try:
            # Build run command with full paths
            # New log structure:
            #   twitter/actions.jsonl - Twitter action log
            #   reddit/actions.jsonl  - Reddit action log
            #   simulation.log        - Main process log
            
            cmd = [
                sys.executable,  # Python interpreter
                script_path,
                "--config", config_path,  # Use full config file path
            ]
            
            # If max_rounds specified, add to command-line arguments
            if max_rounds is not None and max_rounds > 0:
                cmd.extend(["--max-rounds", str(max_rounds)])
            
            # Create main log file to avoid stdout/stderr pipe buffer overflow
            main_log_path = os.path.join(sim_dir, "simulation.log")
            main_log_file = open(main_log_path, 'w', encoding='utf-8')
            
            # Set subprocess environment variables to ensure UTF-8 encoding on Windows
            # This fixes third-party libraries (like OASIS) not specifying encoding when reading files
            env = os.environ.copy()
            env['PYTHONUTF8'] = '1'  # Python 3.7+ support, make all open() use UTF-8 by default
            env['PYTHONIOENCODING'] = 'utf-8'  # Ensure stdout/stderr use UTF-8
            
            # Set working directory to simulation directory (database files etc. will be generated here)
            # Use start_new_session=True to create new process group, ensuring all child processes can be terminated via os.killpg
            # Terminate any existing process handle in cls._processes[simulation_id] if alive
            old_proc = cls._processes.get(simulation_id)
            if old_proc and old_proc.poll() is None:
                logger.warning(f"Terminating existing process handle before Popen: {simulation_id}")
                try:
                    cls._terminate_process(old_proc, simulation_id)
                except Exception as e:
                    logger.warning(f"Error terminating existing process handle: {e}")
            existing_thread = cls._monitor_threads.get(simulation_id)
            if existing_thread and existing_thread.is_alive() and existing_thread != threading.current_thread():
                existing_thread.join(timeout=5.0)
                if not existing_thread.is_alive() and cls._monitor_threads.get(simulation_id) is existing_thread:
                    cls._monitor_threads.pop(simulation_id, None)

            process = subprocess.Popen(
                cmd,
                cwd=sim_dir,
                stdout=main_log_file,
                stderr=subprocess.STDOUT,  # stderr also written to same file
                text=True,
                encoding='utf-8',  # Explicitly specify encoding
                bufsize=1,
                env=env,  # Pass environment variables with UTF-8 settings
                start_new_session=True,  # Create new process group, ensure all related processes terminate when server closes
            )
            
            # Save file handle for later closing
            cls._stdout_files[simulation_id] = main_log_file
            cls._stderr_files[simulation_id] = None  # No longer need separate stderr
            
            state.process_pid = process.pid
            state.runner_status = RunnerStatus.RUNNING
            cls._processes[simulation_id] = process
            cls._save_run_state(state)
            
            # Start monitoring thread
            monitor_thread = threading.Thread(
                target=cls._monitor_simulation,
                args=(simulation_id,),
                daemon=True
            )
            monitor_thread.start()
            cls._monitor_threads[simulation_id] = monitor_thread
            
            logger.info(f"Simulation started successfully: {simulation_id}, pid={process.pid}, platform={platform}")
            
        except Exception as e:
            state.runner_status = RunnerStatus.FAILED
            state.error = str(e)
            cls._save_run_state(state)
            raise
        
        return state
    
    @classmethod
    def _monitor_simulation(cls, simulation_id: str):
        """Monitor simulation process and parse action logs"""
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        
        # New log structure: per-platform action logs
        twitter_actions_log = os.path.join(sim_dir, "twitter", "actions.jsonl")
        reddit_actions_log = os.path.join(sim_dir, "reddit", "actions.jsonl")
        
        process = cls._processes.get(simulation_id)
        state = cls.get_run_state(simulation_id)
        
        if not process or not state:
            return

        monitored_process = process
        monitored_pid = getattr(process, 'pid', None)
        
        twitter_position = 0
        reddit_position = 0
        
        try:
            while monitored_process.poll() is None:  # Process still running
                # Read Twitter action log
                if os.path.exists(twitter_actions_log):
                    twitter_position = cls._read_action_log(
                        twitter_actions_log, twitter_position, state, "twitter"
                    )
                
                # Read Reddit action log
                if os.path.exists(reddit_actions_log):
                    reddit_position = cls._read_action_log(
                        reddit_actions_log, reddit_position, state, "reddit"
                    )
                
                # Update status if still our process
                current_check = cls.get_run_state(simulation_id)
                if current_check and current_check.process_pid != monitored_pid:
                    logger.info(f"Monitor loop detected new process for {simulation_id}, exiting loop")
                    return
                if current_check and current_check.runner_status in [RunnerStatus.STOPPING, RunnerStatus.STOPPED]:
                    state.runner_status = current_check.runner_status
                cls._save_run_state(state)
                time.sleep(2)
            
            # After process ends, read logs one more time
            if os.path.exists(twitter_actions_log):
                cls._read_action_log(twitter_actions_log, twitter_position, state, "twitter")
            if os.path.exists(reddit_actions_log):
                cls._read_action_log(reddit_actions_log, reddit_position, state, "reddit")
            
            # Process ended
            exit_code = monitored_process.returncode
            
            current_check = cls.get_run_state(simulation_id)
            if current_check and current_check.process_pid != monitored_pid:
                logger.warning(
                    f"Not persisting monitor state for {simulation_id}: PID changed "
                    f"({monitored_pid} -> {current_check.process_pid})"
                )
            else:
                target_state = current_check or state
                if target_state.runner_status in [RunnerStatus.STOPPING, RunnerStatus.STOPPED]:
                    target_state.runner_status = RunnerStatus.STOPPED
                    if not target_state.completed_at:
                        target_state.completed_at = datetime.now().isoformat()
                    logger.info(f"Simulation stopped: {simulation_id}")
                elif exit_code == 0:
                    target_state.runner_status = RunnerStatus.COMPLETED
                    if not target_state.completed_at:
                        target_state.completed_at = datetime.now().isoformat()
                    logger.info(f"Simulation completed: {simulation_id}")
                else:
                    target_state.runner_status = RunnerStatus.FAILED
                    # Read error info from main log file
                    main_log_path = os.path.join(sim_dir, "simulation.log")
                    error_info = ""
                    try:
                        if os.path.exists(main_log_path):
                            with open(main_log_path, 'r', encoding='utf-8') as f:
                                error_info = f.read()[-2000:]  # Take last 2000 characters
                    except Exception:
                        pass
                    target_state.error = f"Process exit code: {exit_code}, error: {error_info}"
                    if not target_state.completed_at:
                        target_state.completed_at = datetime.now().isoformat()
                    logger.error(f"Simulation failed: {simulation_id}, error={target_state.error}")
                
                target_state.twitter_running = False
                target_state.reddit_running = False

                if target_state is not state:
                    target_state.twitter_actions_count = state.twitter_actions_count
                    target_state.reddit_actions_count = state.reddit_actions_count
                    target_state.current_round = state.current_round
                    target_state.action_distribution = state.action_distribution
                    target_state.round_summaries = state.round_summaries

                verify_check = cls.get_run_state(simulation_id)
                if not verify_check or verify_check.process_pid == monitored_pid:
                    cls._save_run_state(target_state)
                else:
                    logger.warning(
                        f"Not persisting monitor state for {simulation_id}: PID changed "
                        f"({monitored_pid} -> {verify_check.process_pid})"
                    )
            
        except Exception as e:
            logger.error(f"Monitor thread exception: {simulation_id}, error={str(e)}")
            current_check = cls.get_run_state(simulation_id)
            if current_check and current_check.process_pid != monitored_pid:
                logger.warning(
                    f"Not persisting monitor exception state for {simulation_id}: PID changed "
                    f"({monitored_pid} -> {current_check.process_pid})"
                )
            else:
                target_state = current_check or state
                if target_state.runner_status in [RunnerStatus.STOPPING, RunnerStatus.STOPPED]:
                    target_state.runner_status = RunnerStatus.STOPPED
                    if not target_state.completed_at:
                        target_state.completed_at = datetime.now().isoformat()
                else:
                    target_state.runner_status = RunnerStatus.FAILED
                    target_state.error = str(e)
                    if not target_state.completed_at:
                        target_state.completed_at = datetime.now().isoformat()
                target_state.twitter_running = False
                target_state.reddit_running = False

                verify_check = cls.get_run_state(simulation_id)
                if not verify_check or verify_check.process_pid == monitored_pid:
                    cls._save_run_state(target_state)
        
        finally:
            is_our_process = cls._processes.get(simulation_id) is monitored_process
            
            # Stop graph memory updater
            if is_our_process and cls._graph_memory_enabled.get(simulation_id, False):
                try:
                    GraphMemoryManager.stop_updater(simulation_id)
                    logger.info(f"Graph memory update stopped: simulation_id={simulation_id}")
                except Exception as e:
                    logger.error(f"Failed to stop graph memory updater: {e}")
                cls._graph_memory_enabled.pop(simulation_id, None)
            
            # Clean up process resources
            if is_our_process:
                cls._processes.pop(simulation_id, None)
                cls._action_queues.pop(simulation_id, None)

                # Close log file handle
                if simulation_id in cls._stdout_files:
                    try:
                        cls._stdout_files[simulation_id].close()
                    except Exception:
                        pass
                    cls._stdout_files.pop(simulation_id, None)
                if simulation_id in cls._stderr_files and cls._stderr_files[simulation_id]:
                    try:
                        cls._stderr_files[simulation_id].close()
                    except Exception:
                        pass
                    cls._stderr_files.pop(simulation_id, None)

            # Only pop monitor thread if it is current thread
            if cls._monitor_threads.get(simulation_id) is threading.current_thread():
                cls._monitor_threads.pop(simulation_id, None)
    
    @classmethod
    def _read_action_log(
        cls, 
        log_path: str, 
        position: int, 
        state: SimulationRunState,
        platform: str
    ) -> int:
        """
        Read action log file
        
        Args:
            log_path: Action log file path
            position: Last read position
            state: Run state object
            platform: Platform name (twitter/reddit)
            
        Returns:
            New read position
        """
        # Check if graph memory update is enabled
        sim_id = getattr(state, "simulation_id", "")
        graph_memory_enabled = cls._graph_memory_enabled.get(sim_id, False) if sim_id else False
        graph_updater = None
        if graph_memory_enabled and sim_id:
            graph_updater = GraphMemoryManager.get_updater(sim_id)

        last_valid_offset = position
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                f.seek(position)
                while True:
                    line_start = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    if not line.endswith('\n'):
                        f.seek(line_start)
                        break
                    last_valid_offset = f.tell()

                    line = line.strip()
                    if line:
                        try:
                            action_data = json.loads(line)

                            # Handle event type entries
                            if "event_type" in action_data:
                                event_type = action_data.get("event_type")

                                # Detect simulation_end event, mark platform completed
                                if event_type == "simulation_end":
                                    if platform == "twitter":
                                        state.twitter_completed = True
                                        state.twitter_running = False
                                        logger.info(f"Twitter simulation completed: {getattr(state, 'simulation_id', '')}, total_rounds={action_data.get('total_rounds')}, total_actions={action_data.get('total_actions')}")
                                    elif platform == "reddit":
                                        state.reddit_completed = True
                                        state.reddit_running = False
                                        logger.info(f"Reddit simulation completed: {getattr(state, 'simulation_id', '')}, total_rounds={action_data.get('total_rounds')}, total_actions={action_data.get('total_actions')}")

                                    # Check if all enabled platforms are completed
                                    # If only one platform is running, check only that platform
                                    # If both platforms are running, need both to complete
                                    all_completed = cls._check_all_platforms_completed(state)
                                    if all_completed:
                                        state.runner_status = RunnerStatus.COMPLETED
                                        state.completed_at = datetime.now().isoformat()
                                        logger.info(f"All platform simulations completed: {getattr(state, 'simulation_id', '')}")

                                # Update round information (from round_end event)
                                elif event_type == "round_end":
                                    round_num = action_data.get("round", 0)
                                    simulated_hours = action_data.get("simulated_hours", 0)

                                    # Update per-platform independent rounds and time
                                    if platform == "twitter":
                                        if round_num > state.twitter_current_round:
                                            state.twitter_current_round = round_num
                                        state.twitter_simulated_hours = simulated_hours
                                    elif platform == "reddit":
                                        if round_num > state.reddit_current_round:
                                            state.reddit_current_round = round_num
                                        state.reddit_simulated_hours = simulated_hours

                                    # Overall rounds take maximum of both platforms
                                    if round_num > state.current_round:
                                        state.current_round = round_num
                                    # Overall time takes maximum of both platforms
                                    state.simulated_hours = max(state.twitter_simulated_hours, state.reddit_simulated_hours)

                                continue

                            action = AgentAction(
                                round_num=action_data.get("round", 0),
                                timestamp=action_data.get("timestamp", datetime.now().isoformat()),
                                platform=platform,
                                agent_id=action_data.get("agent_id", 0),
                                agent_name=action_data.get("agent_name", ""),
                                action_type=action_data.get("action_type", ""),
                                action_args=action_data.get("action_args", {}),
                                result=action_data.get("result"),
                                success=action_data.get("success", True),
                            )
                            if hasattr(state, "add_action") and callable(state.add_action):
                                state.add_action(action)
                            elif hasattr(state, "actions") and isinstance(state.actions, list):
                                state.actions.append(action)

                            if hasattr(state, "actions") and isinstance(state.actions, list) and action not in state.actions:
                                state.actions.append(action)

                            # Incrementally cache parsed action
                            cache_sim_id = getattr(state, "simulation_id", str(state))
                            with cls._action_cache_lock:
                                if cache_sim_id not in cls._action_cache:
                                    cls._action_cache[cache_sim_id] = []
                                cls._action_cache[cache_sim_id].append(action)

                            # Update rounds
                            if action.round_num and hasattr(state, "current_round") and action.round_num > state.current_round:
                                state.current_round = action.round_num

                            # If graph memory update enabled, send activity to graph
                            if graph_updater:
                                graph_updater.add_activity_from_dict(action_data, platform)

                        except json.JSONDecodeError:
                            pass
                return last_valid_offset
        except Exception as e:
            logger.warning(f"Failed to read action log: {log_path}, error={e}")
            return last_valid_offset
    
    @classmethod
    def _check_all_platforms_completed(cls, state: SimulationRunState) -> bool:
        """
        Check if all enabled platforms have completed simulation
        
        Judge whether a platform is enabled by checking if corresponding actions.jsonl file exists
        
        Returns:
            True if all enabled platforms are completed
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, state.simulation_id)
        twitter_log = os.path.join(sim_dir, "twitter", "actions.jsonl")
        reddit_log = os.path.join(sim_dir, "reddit", "actions.jsonl")
        
        # Check which platforms are enabled (by file existence)
        twitter_enabled = os.path.exists(twitter_log)
        reddit_enabled = os.path.exists(reddit_log)
        
        # If platform is enabled but not completed, return False
        if twitter_enabled and not state.twitter_completed:
            return False
        if reddit_enabled and not state.reddit_completed:
            return False
        
        # At least one platform is enabled and completed
        return twitter_enabled or reddit_enabled
    
    @staticmethod
    def _is_pid_alive(pid: Optional[int]) -> bool:
        """
        Cross-platform check if a process with given PID is alive.
        Avoids defunct zombies on Unix by checking /proc/{pid}/status.
        """
        if pid is None:
            return False
        try:
            pid = int(pid)
        except (ValueError, TypeError):
            return False
        if pid <= 0:
            return False

        if IS_WINDOWS:
            try:
                res = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                return res.returncode == 0 and str(pid) in res.stdout
            except Exception:
                return False

        try:
            os.kill(pid, 0)
        except OSError:
            return False

        proc_status = f"/proc/{pid}/status"
        if os.path.exists(proc_status):
            try:
                with open(proc_status, "r") as f:
                    for line in f:
                        if line.startswith("State:"):
                            state_val = line.split()[1] if len(line.split()) > 1 else ""
                            if state_val in ("Z", "X") or "zombie" in line.lower():
                                return False
                            break
            except Exception:
                return False

        return True

    @classmethod
    def is_process_alive(cls, simulation_id: str, state: Optional[SimulationRunState] = None) -> bool:
        """
        Check if a simulation process is currently alive.
        Checks in-memory process handle first; if not present, falls back to state.process_pid.
        """
        process = cls._processes.get(simulation_id)
        if process is not None:
            return process.poll() is None

        if state is None:
            state = cls.get_run_state(simulation_id)

        if state and state.process_pid:
            return cls._is_pid_alive(state.process_pid)

        return False

    @classmethod
    def _terminate_pid(cls, pid: int, simulation_id: str, timeout: int = 10):
        """
        Cross-platform terminate process group by PID.
        """
        if IS_WINDOWS:
            logger.info(f"Terminate process tree by PID (Windows): simulation={simulation_id}, pid={pid}")
            try:
                subprocess.run(
                    ['taskkill', '/F', '/PID', str(pid), '/T'],
                    capture_output=True,
                    timeout=timeout
                )
            except Exception as e:
                logger.warning(f"taskkill failed for pid={pid}: {e}")
        else:
            try:
                pgid = os.getpgid(pid)
            except (ProcessLookupError, OSError):
                pgid = pid

            logger.info(f"Terminate process group by PID (Unix): simulation={simulation_id}, pid={pid}, pgid={pgid}")
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                return
            except OSError as e:
                logger.warning(f"SIGTERM killpg failed for pgid={pgid}: {e}")
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass

            start_t = time.time()
            while time.time() - start_t < timeout:
                if not cls._is_pid_alive(pid):
                    return
                time.sleep(0.1)

            logger.warning(f"Process group {pgid} not responding to SIGTERM, force terminating: {simulation_id}")
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                return
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

            start_t = time.time()
            while time.time() - start_t < 2:
                if not cls._is_pid_alive(pid):
                    return
                time.sleep(0.1)

    @classmethod
    def _terminate_process(cls, process: subprocess.Popen, simulation_id: str, timeout: int = 10):
        """
        Cross-platform terminate process and its child processes
        
        Args:
            process: Process to terminate
            simulation_id: Simulation ID (for logging)
            timeout: Timeout for process exit (seconds)
        """
        if IS_WINDOWS:
            # Windows: Use taskkill command to terminate process tree
            # /F = force terminate, /T = terminate process tree (including child processes)
            logger.info(f"Terminate process tree (Windows): simulation={simulation_id}, pid={process.pid}")
            try:
                # Try graceful termination first
                subprocess.run(
                    ['taskkill', '/PID', str(process.pid), '/T'],
                    capture_output=True,
                    timeout=5
                )
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    # Force terminate
                    logger.warning(f"Process not responding, force terminating: {simulation_id}")
                    subprocess.run(
                        ['taskkill', '/F', '/PID', str(process.pid), '/T'],
                        capture_output=True,
                        timeout=5
                    )
                    process.wait(timeout=5)
            except Exception as e:
                logger.warning(f"taskkill failed, trying terminate: {e}")
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        else:
            # Unix: Use process group termination
            # Since start_new_session=True, process group ID equals main process PID
            pgid = os.getpgid(process.pid)
            logger.info(f"Terminate process group (Unix): simulation={simulation_id}, pgid={pgid}")
            
            # First send SIGTERM to the entire process group
            os.killpg(pgid, signal.SIGTERM)
            
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # If still not ended after timeout, force send SIGKILL
                logger.warning(f"Process group not responding to SIGTERM, force terminating: {simulation_id}")
                os.killpg(pgid, signal.SIGKILL)
                process.wait(timeout=5)
    
    @classmethod
    def stop_simulation(cls, simulation_id: str) -> SimulationRunState:
        """Stop simulation"""
        state = cls.get_run_state(simulation_id)
        if not state:
            raise ValueError(f"Simulation does not exist: {simulation_id}")

        is_alive = cls.is_process_alive(simulation_id, state)
        if not is_alive and state.runner_status not in [RunnerStatus.RUNNING, RunnerStatus.PAUSED]:
            raise ValueError(f"Simulation not running: {simulation_id}, status={state.runner_status}")

        state.runner_status = RunnerStatus.STOPPING
        cls._save_run_state(state)

        # Terminate process
        process = cls._processes.get(simulation_id)
        if process and process.poll() is None:
            try:
                cls._terminate_process(process, simulation_id)
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.error(f"Failed to terminate process group: {simulation_id}, error={e}")
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
        elif state.process_pid and cls._is_pid_alive(state.process_pid):
            try:
                cls._terminate_pid(state.process_pid, simulation_id)
            except Exception as e:
                logger.error(f"Failed to terminate process PID {state.process_pid}: {simulation_id}, error={e}")

        # Wait for monitor thread to complete
        monitor_thread = cls._monitor_threads.get(simulation_id)
        if monitor_thread and monitor_thread.is_alive() and monitor_thread != threading.current_thread():
            monitor_thread.join(timeout=5.0)
            if not monitor_thread.is_alive() and cls._monitor_threads.get(simulation_id) is monitor_thread:
                cls._monitor_threads.pop(simulation_id, None)

        if cls._processes.get(simulation_id) is process:
            cls._processes.pop(simulation_id, None)

        if simulation_id in cls._stdout_files and cls._processes.get(simulation_id) is None:
            try:
                cls._stdout_files[simulation_id].close()
            except Exception:
                pass
            cls._stdout_files.pop(simulation_id, None)

        latest_state = cls.get_run_state(simulation_id) or state
        latest_state.runner_status = RunnerStatus.STOPPED
        latest_state.twitter_running = False
        latest_state.reddit_running = False
        if not latest_state.completed_at:
            latest_state.completed_at = datetime.now().isoformat()
        cls._save_run_state(latest_state)

        # Stop graph memory updater
        if cls._graph_memory_enabled.get(simulation_id, False):
            try:
                GraphMemoryManager.stop_updater(simulation_id)
                logger.info(f"Graph memory update stopped: simulation_id={simulation_id}")
            except Exception as e:
                logger.error(f"Failed to stop graph memory updater: {e}")
            cls._graph_memory_enabled.pop(simulation_id, None)

        logger.info(f"Simulation stopped: {simulation_id}")
        return state
    
    @classmethod
    def _read_actions_from_file(
        cls,
        file_path: str,
        default_platform: Optional[str] = None,
        platform_filter: Optional[str] = None,
        agent_id: Optional[int] = None,
        round_num: Optional[int] = None
    ) -> List[AgentAction]:
        """
        Read actions from single action file
        
        Args:
            file_path: Action log file path
            default_platform: Default platform (used when action record lacks platform field)
            platform_filter: Filter platform
            agent_id: Filter Agent ID
            round_num: Filter round
        """
        if not os.path.exists(file_path):
            return []
        
        actions = []
        
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                try:
                    data = json.loads(line)
                    
                    # Skip non-action records (such as simulation_start, round_start, round_end events)
                    if "event_type" in data:
                        continue
                    
                    # Skip records without agent_id (non-Agent actions)
                    if "agent_id" not in data:
                        continue
                    
                    # Get platform: prefer platform in record, otherwise use default platform
                    record_platform = data.get("platform") or default_platform or ""
                    
                    # Filter
                    if platform_filter and record_platform != platform_filter:
                        continue
                    if agent_id is not None and data.get("agent_id") != agent_id:
                        continue
                    if round_num is not None and data.get("round") != round_num:
                        continue
                    
                    actions.append(AgentAction(
                        round_num=data.get("round", 0),
                        timestamp=data.get("timestamp", ""),
                        platform=record_platform,
                        agent_id=data.get("agent_id", 0),
                        agent_name=data.get("agent_name", ""),
                        action_type=data.get("action_type", ""),
                        action_args=data.get("action_args", {}),
                        result=data.get("result"),
                        success=data.get("success", True),
                    ))
                    
                except json.JSONDecodeError:
                    continue
        
        return actions
    
    @classmethod
    def get_all_actions(
        cls,
        simulation_id: str,
        platform: Optional[str] = None,
        agent_id: Optional[int] = None,
        round_num: Optional[int] = None
    ) -> List[AgentAction]:
        """
        Get complete action history for all platforms (no pagination limit)

        Args:
            simulation_id: Simulation ID
            platform: Filter platform (twitter/reddit)
            agent_id: Filter Agent
            round_num: Filter round

        Returns:
            Complete action list (sorted by timestamp, newest first)
        """
        with cls._action_cache_lock:
            cached = cls._action_cache.get(simulation_id)
            if cached is not None and len(cached) > 0:
                cached_actions = list(cached)
            else:
                cached_actions = None

        if cached_actions is None:
            # Cache is empty/cold (e.g. server restart), load from disk files
            sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
            disk_actions = []

            # Read Twitter action file
            twitter_actions_log = os.path.join(sim_dir, "twitter", "actions.jsonl")
            if os.path.exists(twitter_actions_log):
                disk_actions.extend(cls._read_actions_from_file(
                    twitter_actions_log,
                    default_platform="twitter"
                ))

            # Read Reddit action file
            reddit_actions_log = os.path.join(sim_dir, "reddit", "actions.jsonl")
            if os.path.exists(reddit_actions_log):
                disk_actions.extend(cls._read_actions_from_file(
                    reddit_actions_log,
                    default_platform="reddit"
                ))

            # If per-platform files do not exist, try reading old single file format
            if not disk_actions:
                actions_log = os.path.join(sim_dir, "actions.jsonl")
                if os.path.exists(actions_log):
                    disk_actions = cls._read_actions_from_file(
                        actions_log,
                        default_platform=None
                    )

            with cls._action_cache_lock:
                if simulation_id not in cls._action_cache or len(cls._action_cache[simulation_id]) == 0:
                    cls._action_cache[simulation_id] = list(disk_actions)
                cached_actions = list(cls._action_cache[simulation_id])

        # Filter in memory
        actions = [
            a for a in cached_actions
            if (platform is None or a.platform == platform)
            and (agent_id is None or a.agent_id == agent_id)
            and (round_num is None or a.round_num == round_num)
        ]

        # Sort by timestamp (newest first)
        actions.sort(key=lambda x: x.timestamp, reverse=True)

        return actions
    
    @classmethod
    def get_actions(
        cls,
        simulation_id: str,
        limit: int = 100,
        offset: int = 0,
        platform: Optional[str] = None,
        agent_id: Optional[int] = None,
        round_num: Optional[int] = None
    ) -> List[AgentAction]:
        """
        Get action history (with pagination)
        
        Args:
            simulation_id: Simulation ID
            limit: Return count limit
            offset: Offset
            platform: Filter platform
            agent_id: Filter Agent
            round_num: Filter round
            
        Returns:
            Action list
        """
        actions = cls.get_all_actions(
            simulation_id=simulation_id,
            platform=platform,
            agent_id=agent_id,
            round_num=round_num
        )
        
        # Pagination
        return actions[offset:offset + limit]
    
    @classmethod
    def get_timeline(
        cls,
        simulation_id: str,
        start_round: int = 0,
        end_round: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get simulation timeline (summarized by rounds)
        
        Args:
            simulation_id: Simulation ID
            start_round: Start round
            end_round: End round
            
        Returns:
            Summary information for each round
        """
        actions = cls.get_actions(simulation_id, limit=10000)
        
        # Group by round
        rounds: Dict[int, Dict[str, Any]] = {}
        
        for action in actions:
            round_num = action.round_num
            
            if round_num < start_round:
                continue
            if end_round is not None and round_num > end_round:
                continue
            
            if round_num not in rounds:
                rounds[round_num] = {
                    "round_num": round_num,
                    "twitter_actions": 0,
                    "reddit_actions": 0,
                    "active_agents": set(),
                    "action_types": {},
                    "first_action_time": action.timestamp,
                    "last_action_time": action.timestamp,
                }
            
            r = rounds[round_num]
            
            if action.platform == "twitter":
                r["twitter_actions"] += 1
            else:
                r["reddit_actions"] += 1
            
            r["active_agents"].add(action.agent_id)
            r["action_types"][action.action_type] = r["action_types"].get(action.action_type, 0) + 1
            r["last_action_time"] = action.timestamp
        
        # Convert to list
        result = []
        for round_num in sorted(rounds.keys()):
            r = rounds[round_num]
            result.append({
                "round_num": round_num,
                "twitter_actions": r["twitter_actions"],
                "reddit_actions": r["reddit_actions"],
                "total_actions": r["twitter_actions"] + r["reddit_actions"],
                "active_agents_count": len(r["active_agents"]),
                "active_agents": list(r["active_agents"]),
                "action_types": r["action_types"],
                "first_action_time": r["first_action_time"],
                "last_action_time": r["last_action_time"],
            })
        
        return result
    
    @classmethod
    def get_agent_stats(cls, simulation_id: str) -> List[Dict[str, Any]]:
        """
        Get statistics for each Agent
        
        Returns:
            Agent statistics list
        """
        actions = cls.get_actions(simulation_id, limit=10000)
        
        agent_stats: Dict[int, Dict[str, Any]] = {}
        
        for action in actions:
            agent_id = action.agent_id
            
            if agent_id not in agent_stats:
                agent_stats[agent_id] = {
                    "agent_id": agent_id,
                    "agent_name": action.agent_name,
                    "total_actions": 0,
                    "twitter_actions": 0,
                    "reddit_actions": 0,
                    "action_types": {},
                    "first_action_time": action.timestamp,
                    "last_action_time": action.timestamp,
                }
            
            stats = agent_stats[agent_id]
            stats["total_actions"] += 1
            
            if action.platform == "twitter":
                stats["twitter_actions"] += 1
            else:
                stats["reddit_actions"] += 1
            
            stats["action_types"][action.action_type] = stats["action_types"].get(action.action_type, 0) + 1
            stats["last_action_time"] = action.timestamp
        
        # Sort by total actions
        result = sorted(agent_stats.values(), key=lambda x: x["total_actions"], reverse=True)
        
        return result
    
    @classmethod
    def cleanup_simulation_logs(cls, simulation_id: str) -> Dict[str, Any]:
        """
        Clean up simulation run logs (for force restart)
        
        Will delete the following files:
        - run_state.json
        - twitter/actions.jsonl
        - reddit/actions.jsonl
        - simulation.log
        - stdout.log / stderr.log
        - twitter_simulation.db (simulation database)
        - reddit_simulation.db (simulation database)
        - env_status.json (environment status)
        
        Note: Does not delete config files (simulation_config.json) and profile files
        
        Args:
            simulation_id: Simulation ID
            
        Returns:
            Cleanup result information
        """
        import shutil
        
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        
        if not os.path.exists(sim_dir):
            return {"success": True, "message": "Simulation directory does not exist, no cleanup needed"}
        
        cleaned_files = []
        errors = []
        
        # Files to delete (including database files)
        files_to_delete = [
            "run_state.json",
            "simulation.log",
            "stdout.log",
            "stderr.log",
            "twitter_simulation.db",  # Twitter platform database
            "reddit_simulation.db",   # Reddit platform database
            "env_status.json",        # Environment status file
        ]
        
        # Directories to delete (contains action logs)
        dirs_to_clean = ["twitter", "reddit"]
        
        # Delete files
        for filename in files_to_delete:
            file_path = os.path.join(sim_dir, filename)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    cleaned_files.append(filename)
                except Exception as e:
                    errors.append(f"Failed to delete {filename}: {str(e)}")
        
        # Clean up action logs in platform directories
        for dir_name in dirs_to_clean:
            dir_path = os.path.join(sim_dir, dir_name)
            if os.path.exists(dir_path):
                actions_file = os.path.join(dir_path, "actions.jsonl")
                if os.path.exists(actions_file):
                    try:
                        os.remove(actions_file)
                        cleaned_files.append(f"{dir_name}/actions.jsonl")
                    except Exception as e:
                        errors.append(f"Failed to delete {dir_name}/actions.jsonl: {str(e)}")
        
        # Clean up in-memory run state
        if simulation_id in cls._run_states:
            del cls._run_states[simulation_id]
        with cls._action_cache_lock:
            cls._action_cache.pop(simulation_id, None)
        with cls._state_save_lock:
            cls._last_saved_state.pop(simulation_id, None)
        
        logger.info(f"Cleanup simulation logs completed: {simulation_id}, deleted files: {cleaned_files}")
        
        return {
            "success": len(errors) == 0,
            "cleaned_files": cleaned_files,
            "errors": errors if errors else None
        }
    
    # Flag to prevent duplicate cleanup
    _cleanup_done = False
    
    @classmethod
    def cleanup_all_simulations(cls):
        """
        Clean up all running simulation processes
        
        Called when server closes, ensures all child processes are terminated
        """
        # Prevent duplicate cleanup
        if cls._cleanup_done:
            return
        cls._cleanup_done = True
        
        # Check if there is content to clean (avoid empty process printing useless logs)
        has_processes = bool(cls._processes)
        has_updaters = bool(cls._graph_memory_enabled)
        has_threads = any(t and t.is_alive() for t in cls._monitor_threads.values())
        
        if not has_processes and not has_updaters and not has_threads:
            return  # No content to clean, return silently
        
        logger.info("Cleaning up all simulation processes...")
        
        # First stop all graph memory updaters (stop_all prints logs internally)
        try:
            GraphMemoryManager.stop_all()
        except Exception as e:
            logger.error(f"Failed to stop graph memory updater: {e}")
        cls._graph_memory_enabled.clear()
        
        # Copy dict to avoid modification during iteration
        processes = list(cls._processes.items())
        
        for simulation_id, process in processes:
            try:
                if process.poll() is None:  # Process still running
                    # Mark state as STOPPED before terminating process so monitor thread won't set FAILED
                    state = cls.get_run_state(simulation_id)
                    if state:
                        state.runner_status = RunnerStatus.STOPPED
                        state.twitter_running = False
                        state.reddit_running = False
                        state.completed_at = datetime.now().isoformat()
                        state.error = "Server closed, simulation terminated"
                        cls._save_run_state(state)

                    logger.info(f"Terminate simulation process: {simulation_id}, pid={process.pid}")
                    
                    try:
                        # Use cross-platform process termination method
                        cls._terminate_process(process, simulation_id, timeout=5)
                    except (ProcessLookupError, OSError):
                        # Process may no longer exist, try direct termination
                        try:
                            process.terminate()
                            process.wait(timeout=3)
                        except Exception:
                            process.kill()
                    
                    # Update run_state.json
                    state = cls.get_run_state(simulation_id)
                    if state:
                        state.runner_status = RunnerStatus.STOPPED
                        state.twitter_running = False
                        state.reddit_running = False
                        state.completed_at = datetime.now().isoformat()
                        state.error = "Server closed, simulation terminated"
                        cls._save_run_state(state)
                    
                    # Also update state.json, set status to stopped
                    try:
                        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
                        state_file = os.path.join(sim_dir, "state.json")
                        logger.info(f"Attempting to update state.json: {state_file}")
                        if os.path.exists(state_file):
                            with open(state_file, 'r', encoding='utf-8') as f:
                                state_data = json.load(f)
                            state_data['status'] = 'stopped'
                            state_data['updated_at'] = datetime.now().isoformat()
                            with open(state_file, 'w', encoding='utf-8') as f:
                                json.dump(state_data, f, indent=2, ensure_ascii=False)
                            logger.info(f"Updated state.json status to stopped: {simulation_id}")
                        else:
                            logger.warning(f"state.json does not exist: {state_file}")
                    except Exception as state_err:
                        logger.warning(f"Failed to update state.json: {simulation_id}, error={state_err}")
                        
            except Exception as e:
                logger.error(f"Failed to clean up process: {simulation_id}, error={e}")
        
        # Wait for all monitor threads to complete gracefully
        for simulation_id, thread in list(cls._monitor_threads.items()):
            if thread and thread.is_alive() and thread != threading.current_thread():
                try:
                    thread.join(timeout=5.0)
                except Exception as e:
                    logger.warning(f"Error joining monitor thread for {simulation_id}: {e}")
        cls._monitor_threads.clear()

        # Clean up file handles
        for simulation_id, file_handle in list(cls._stdout_files.items()):
            try:
                if file_handle:
                    file_handle.close()
            except Exception:
                pass
        cls._stdout_files.clear()
        
        for simulation_id, file_handle in list(cls._stderr_files.items()):
            try:
                if file_handle:
                    file_handle.close()
            except Exception:
                pass
        cls._stderr_files.clear()
        
        # Clean up in-memory state
        cls._processes.clear()
        cls._action_queues.clear()
        with cls._action_cache_lock:
            cls._action_cache.clear()
        with cls._state_save_lock:
            cls._last_saved_state.clear()
        
        logger.info("Simulation process cleanup completed")
    
    @classmethod
    def register_cleanup(cls):
        """
        Register cleanup function
        
        Called when Flask app starts, ensures all simulation processes are cleaned when server closes
        """
        global _cleanup_registered
        
        if _cleanup_registered:
            return
        
        # In Flask debug mode, only register cleanup in reloader child process (process actually running the app)
        # WERKZEUG_RUN_MAIN=true indicates it is a reloader child process
        # If not in debug mode, no such environment variable, also need to register
        is_reloader_process = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
        is_debug_mode = os.environ.get('FLASK_DEBUG') == '1' or os.environ.get('WERKZEUG_RUN_MAIN') is not None
        
        # In debug mode, only register in reloader child process; always register in non-debug mode
        if is_debug_mode and not is_reloader_process:
            _cleanup_registered = True  # Mark as registered, prevent child process from trying again
            return
        
        # Save original signal handler
        original_sigint = signal.getsignal(signal.SIGINT)
        original_sigterm = signal.getsignal(signal.SIGTERM)
        # SIGHUP only exists on Unix systems (macOS/Linux), not on Windows
        original_sighup = None
        has_sighup = hasattr(signal, 'SIGHUP')
        if has_sighup:
            original_sighup = signal.getsignal(signal.SIGHUP)
        
        def cleanup_handler(signum=None, frame=None):
            """Signal handler: clean up simulation processes first, then call original handler"""
            # Only print logs if there are processes to clean
            if cls._processes or cls._graph_memory_enabled:
                logger.info(f"Received signal {signum}, starting cleanup...")
            cls.cleanup_all_simulations()
            
            # Call original signal handler, let Flask exit normally
            if signum == signal.SIGINT and callable(original_sigint):
                original_sigint(signum, frame)
            elif signum == signal.SIGTERM and callable(original_sigterm):
                original_sigterm(signum, frame)
            elif has_sighup and signum == signal.SIGHUP:
                # SIGHUP: Sent when terminal closes
                if callable(original_sighup):
                    original_sighup(signum, frame)
                else:
                    # Default behavior: exit normally
                    sys.exit(0)
            else:
                # If original handler not callable (such as SIG_DFL), use default behavior
                raise KeyboardInterrupt
        
        # Register atexit handler (as fallback)
        atexit.register(cls.cleanup_all_simulations)
        
        # Register signal handler (only in main thread)
        try:
            # SIGTERM: default signal for kill command
            signal.signal(signal.SIGTERM, cleanup_handler)
            # SIGINT: Ctrl+C
            signal.signal(signal.SIGINT, cleanup_handler)
            # SIGHUP: terminal close (Unix only)
            if has_sighup:
                signal.signal(signal.SIGHUP, cleanup_handler)
        except ValueError:
            # Not in main thread, can only use atexit
            logger.warning("Cannot register signal handler (not in main thread), only using atexit")
        
        _cleanup_registered = True
    
    @classmethod
    def get_running_simulations(cls) -> List[str]:
        """
        Get list of all running simulation IDs
        """
        running = []
        for sim_id, process in cls._processes.items():
            if process.poll() is None:
                running.append(sim_id)
        return running
    
    # ============== Interview functionality ==============
    
    @classmethod
    def check_env_alive(cls, simulation_id: str) -> bool:
        """
        Check if simulation environment is alive (can receive Interview commands)

        Args:
            simulation_id: Simulation ID

        Returns:
            True means environment is alive, False means environment is closed
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        if not os.path.exists(sim_dir):
            return False

        ipc_client = SimulationIPCClient(sim_dir)
        return ipc_client.check_env_alive()

    @classmethod
    def get_env_status_detail(cls, simulation_id: str) -> Dict[str, Any]:
        """
        Get detailed status information of simulation environment

        Args:
            simulation_id: Simulation ID

        Returns:
            Status details dict, contains status, twitter_available, reddit_available, timestamp
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        status_file = os.path.join(sim_dir, "env_status.json")
        
        default_status = {
            "status": "stopped",
            "twitter_available": False,
            "reddit_available": False,
            "timestamp": None
        }
        
        if not os.path.exists(status_file):
            return default_status
        
        try:
            with open(status_file, 'r', encoding='utf-8') as f:
                status = json.load(f)
            return {
                "status": status.get("status", "stopped"),
                "twitter_available": status.get("twitter_available", False),
                "reddit_available": status.get("reddit_available", False),
                "timestamp": status.get("timestamp")
            }
        except (json.JSONDecodeError, OSError):
            return default_status

    @classmethod
    def interview_agent(
        cls,
        simulation_id: str,
        agent_id: int,
        prompt: str,
        platform: str = None,
        timeout: float = 60.0
    ) -> Dict[str, Any]:
        """
        Interview single Agent

        Args:
            simulation_id: Simulation ID
            agent_id: Agent ID
            prompt: Interview question
            platform: Specify platform (optional)
                - "twitter": only interview Twitter platform
                - "reddit": only interview Reddit platform
                - None: interview both platforms simultaneously in dual-platform simulations, return integrated results
            timeout: Timeout (seconds)

        Returns:
            Interview result dict

        Raises:
            ValueError: Simulation does not exist or environment not running
            TimeoutError: Timeout waiting for response
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        if not os.path.exists(sim_dir):
            raise ValueError(f"Simulation does not exist: {simulation_id}")

        ipc_client = SimulationIPCClient(sim_dir)

        if not ipc_client.check_env_alive():
            raise ValueError(f"Simulation environment not running or closed, cannot execute Interview: {simulation_id}")

        logger.info(f"Send Interview command: simulation_id={simulation_id}, agent_id={agent_id}, platform={platform}")

        response = ipc_client.send_interview(
            agent_id=agent_id,
            prompt=prompt,
            platform=platform,
            timeout=timeout
        )

        if response.status.value == "completed":
            return {
                "success": True,
                "agent_id": agent_id,
                "prompt": prompt,
                "result": response.result,
                "timestamp": response.timestamp
            }
        else:
            return {
                "success": False,
                "agent_id": agent_id,
                "prompt": prompt,
                "error": response.error,
                "timestamp": response.timestamp
            }
    
    @classmethod
    def interview_agents_batch(
        cls,
        simulation_id: str,
        interviews: List[Dict[str, Any]],
        platform: str = None,
        timeout: float = 120.0
    ) -> Dict[str, Any]:
        """
        Batch interview multiple Agents

        Args:
            simulation_id: Simulation ID
            interviews: List of interviews, each element contains {"agent_id": int, "prompt": str, "platform": str(optional)}
            platform: Default platform (optional, overridden by each interview item's platform)
                - "twitter": default only interview Twitter platform
                - "reddit": default only interview Reddit platform
                - None: interview each Agent on both platforms simultaneously in dual-platform simulations
            timeout: Timeout (seconds)

        Returns:
            Batch interview result dict

        Raises:
            ValueError: Simulation does not exist or environment not running
            TimeoutError: Timeout waiting for response
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        if not os.path.exists(sim_dir):
            raise ValueError(f"Simulation does not exist: {simulation_id}")

        ipc_client = SimulationIPCClient(sim_dir)

        if not ipc_client.check_env_alive():
            raise ValueError(f"Simulation environment not running or closed, cannot execute Interview: {simulation_id}")

        logger.info(f"Send batch Interview command: simulation_id={simulation_id}, count={len(interviews)}, platform={platform}")

        response = ipc_client.send_batch_interview(
            interviews=interviews,
            platform=platform,
            timeout=timeout
        )

        if response.status.value == "completed":
            return {
                "success": True,
                "interviews_count": len(interviews),
                "result": response.result,
                "timestamp": response.timestamp
            }
        else:
            return {
                "success": False,
                "interviews_count": len(interviews),
                "error": response.error,
                "timestamp": response.timestamp
            }
    
    @classmethod
    def interview_all_agents(
        cls,
        simulation_id: str,
        prompt: str,
        platform: str = None,
        timeout: float = 180.0
    ) -> Dict[str, Any]:
        """
        Interview all Agents (global interview)

        Interview all Agents in the simulation using the same question

        Args:
            simulation_id: Simulation ID
            prompt: Interview question (all Agents use the same question)
            platform: Specify platform (optional)
                - "twitter": only interview Twitter platform
                - "reddit": only interview Reddit platform
                - None: interview each Agent on both platforms simultaneously in dual-platform simulations
            timeout: Timeout (seconds)

        Returns:
            Global interview result dict
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        if not os.path.exists(sim_dir):
            raise ValueError(f"Simulation does not exist: {simulation_id}")

        # Get all Agent information from config file
        config_path = os.path.join(sim_dir, "simulation_config.json")
        if not os.path.exists(config_path):
            raise ValueError(f"Simulation config does not exist: {simulation_id}")

        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        agent_configs = config.get("agent_configs", [])
        if not agent_configs:
            raise ValueError(f"No agents in simulation config: {simulation_id}")

        # Build batch interview list
        interviews = []
        for agent_config in agent_configs:
            agent_id = agent_config.get("agent_id")
            if agent_id is not None:
                interviews.append({
                    "agent_id": agent_id,
                    "prompt": prompt
                })

        logger.info(f"Send global Interview command: simulation_id={simulation_id}, agent_count={len(interviews)}, platform={platform}")

        return cls.interview_agents_batch(
            simulation_id=simulation_id,
            interviews=interviews,
            platform=platform,
            timeout=timeout
        )
    
    @classmethod
    def close_simulation_env(
        cls,
        simulation_id: str,
        timeout: float = 30.0
    ) -> Dict[str, Any]:
        """
        Close simulation environment (not stop simulation process)
        
        Send close environment command to simulation to gracefully exit command wait mode
        
        Args:
            simulation_id: Simulation ID
            timeout: Timeout (seconds)
            
        Returns:
            Operation result dict
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        if not os.path.exists(sim_dir):
            raise ValueError(f"Simulation does not exist: {simulation_id}")
        
        ipc_client = SimulationIPCClient(sim_dir)
        
        if not ipc_client.check_env_alive():
            return {
                "success": True,
                "message": "Environment already closed"
            }
        
        logger.info(f"Send close environment command: simulation_id={simulation_id}")
        
        try:
            response = ipc_client.send_close_env(timeout=timeout)
            
            return {
                "success": response.status.value == "completed",
                "message": "Close environment command sent",
                "result": response.result,
                "timestamp": response.timestamp
            }
        except TimeoutError:
            # Timeout may be because environment is closing
            return {
                "success": True,
                "message": "Close environment command sent (timeout waiting for response, environment may be closing)"
            }
    
    @classmethod
    def _get_interview_history_from_db(
        cls,
        db_path: str,
        platform_name: str,
        agent_id: Optional[int] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get Interview history from single database"""
        import sqlite3
        
        if not os.path.exists(db_path):
            return []
        
        results = []
        
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            if agent_id is not None:
                cursor.execute("""
                    SELECT user_id, info, created_at
                    FROM trace
                    WHERE action = 'interview' AND user_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (agent_id, limit))
            else:
                cursor.execute("""
                    SELECT user_id, info, created_at
                    FROM trace
                    WHERE action = 'interview'
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (limit,))
            
            for user_id, info_json, created_at in cursor.fetchall():
                try:
                    info = json.loads(info_json) if info_json else {}
                except json.JSONDecodeError:
                    info = {"raw": info_json}
                
                results.append({
                    "agent_id": user_id,
                    "response": info.get("response", info),
                    "prompt": info.get("prompt", ""),
                    "timestamp": created_at,
                    "platform": platform_name
                })
            
            conn.close()
            
        except Exception as e:
            logger.error(f"Failed to read Interview history ({platform_name}): {e}")
        
        return results

    @classmethod
    def get_interview_history(
        cls,
        simulation_id: str,
        platform: str = None,
        agent_id: Optional[int] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get Interview history records (read from database)
        
        Args:
            simulation_id: Simulation ID
            platform: Platform type (reddit/twitter/None)
                - "reddit": only get Reddit platform history
                - "twitter": only get Twitter platform history
                - None: get all history from both platforms
            agent_id: Specify Agent ID (optional, only get history for that Agent)
            limit: Return count limit per platform
            
        Returns:
            Interview history records list
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        
        results = []
        
        # Determine platforms to query
        if platform in ("reddit", "twitter"):
            platforms = [platform]
        else:
            # When platform not specified, query both platforms
            platforms = ["twitter", "reddit"]
        
        for p in platforms:
            db_path = os.path.join(sim_dir, f"{p}_simulation.db")
            platform_results = cls._get_interview_history_from_db(
                db_path=db_path,
                platform_name=p,
                agent_id=agent_id,
                limit=limit
            )
            results.extend(platform_results)
        
        # Sort by time in descending order
        results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        
        # If queried multiple platforms, limit total count
        if len(platforms) > 1 and len(results) > limit:
            results = results[:limit]
        
        return results

