import socket
import threading
import json
import os
import ctypes
import ctypes.wintypes
from pathlib import Path
from typing import Callable

from .logging_config import get_logger

logger = get_logger(__name__)

_server_socket: socket.socket | None = None
_registered_pid: int | None = None
_primary_mutex: int | None = None  # Win32 HANDLE

_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
_user32 = ctypes.WinDLL('user32', use_last_error=True)

_ERROR_ALREADY_EXISTS = 183
_PRIMARY_MUTEX_NAME = "Local\\UniteDocs-Primary"


def try_become_primary() -> bool:
    """Forsøg at vinde named mutex-kapløbet. Returnerer True hvis denne proces er primær."""
    global _primary_mutex
    handle = _kernel32.CreateMutexW(None, True, _PRIMARY_MUTEX_NAME)
    if not handle:
        return True  # Kan ikke oprette — antag primær som fallback
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return False
    _primary_mutex = handle
    return True


def _release_primary_mutex() -> None:
    global _primary_mutex
    if _primary_mutex:
        _kernel32.CloseHandle(_primary_mutex)
        _primary_mutex = None


def _instances_path() -> Path:
    from . import utils
    return utils.get_app_data_path() / "instances.json"


def _read_instances() -> list[dict]:
    try:
        with open(_instances_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _write_instances(instances: list[dict]) -> None:
    path = _instances_path()
    try:
        tmp = path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(instances, f)
        tmp.replace(path)
    except OSError as e:
        logger.error("Kunne ikke skrive instances.json: %s", e)


def _pid_alive(pid: int) -> bool:
    SYNCHRONIZE = 0x00100000
    handle = _kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if handle:
        _kernel32.CloseHandle(handle)
        return True
    return False


def _find_topmost_hwnd(known_hwnds: set[int]) -> int | None:
    result: list[int] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool,
                                          ctypes.wintypes.HWND,
                                          ctypes.wintypes.LPARAM)

    def callback(hwnd, _):
        if hwnd in known_hwnds:
            result.append(hwnd)
            return False  # stop — første hit er øverst i Z-orden
        return True

    _user32.EnumWindows(EnumWindowsProc(callback), 0)
    return result[0] if result else None


def register_starting() -> None:
    """Skriv placeholder-entry så sekundærer ved at en primær er ved at starte."""
    pid = os.getpid()
    instances = [i for i in _read_instances() if _pid_alive(i.get('pid', 0))]
    if not any(i.get('pid') == pid for i in instances):
        instances.append({'pid': pid, 'port': None, 'hwnd': None})
        _write_instances(instances)


def try_forward_to_existing(file_paths: list[str]) -> bool | None:
    """
    Forsøg at sende file_paths til en kørende instans.
    Returnerer True (sendt), False (ingen instans), None (instans starter stadig).
    """
    instances = _read_instances()
    current_pid = os.getpid()
    live = [i for i in instances
            if i.get('pid') != current_pid and _pid_alive(i.get('pid', 0))]

    if not live:
        return False  # Ingen live instans — åbn nyt vindue

    # Er der en instans der er ved at starte (ingen port endnu)?
    if any(i.get('port') is None for i in live):
        return None  # Vent — primær starter stadig

    # Find topmost og forward
    known_hwnds = {i['hwnd'] for i in live if i.get('hwnd')}
    target = None
    if known_hwnds:
        topmost = _find_topmost_hwnd(known_hwnds)
        if topmost:
            target = next((i for i in live if i.get('hwnd') == topmost), None)
    if target is None:
        target = next((i for i in live if i.get('port')), None)
    if target is None:
        return None

    port = target.get('port')
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=2.0) as sock:
            for path in file_paths:
                sock.sendall((path + '\n').encode('utf-8'))
        return True
    except OSError as e:
        logger.warning("Send fejlede (port %s): %s", port, e)
        return False


def _handle_connection(conn: socket.socket,
                        schedule_callback: Callable[[list[str]], None]) -> None:
    paths: list[str] = []
    try:
        buf = b''
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        for line in buf.decode('utf-8').splitlines():
            line = line.strip()
            if line:
                paths.append(line)
    except OSError:
        pass
    finally:
        conn.close()
    if paths:
        schedule_callback(paths)


def start_ipc_server(schedule_callback: Callable[[list[str]], None], hwnd: int) -> None:
    """Bind TCP-server på tilfældig port, registrer i instances.json, start lyttertråd."""
    global _server_socket, _registered_pid
    try_become_primary()  # Overtag mutex hvis ikke allerede holdt

    pid = os.getpid()
    _registered_pid = pid

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(5)
    _server_socket = srv
    port = srv.getsockname()[1]

    # Opdater eksisterende placeholder (fra register_starting) eller tilføj ny entry
    instances = [i for i in _read_instances() if _pid_alive(i.get('pid', 0))]
    for inst in instances:
        if inst.get('pid') == pid:
            inst['port'] = port
            inst['hwnd'] = hwnd
            break
    else:
        instances.append({'pid': pid, 'port': port, 'hwnd': hwnd})
    _write_instances(instances)

    def _accept_loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            threading.Thread(target=_handle_connection,
                             args=(conn, schedule_callback),
                             daemon=True).start()

    threading.Thread(target=_accept_loop, daemon=True).start()


def update_hwnd(new_hwnd: int) -> None:
    """Opdater dette vindues HWND i instances.json (bruges efter UI-genopbygning)."""
    pid = os.getpid()
    instances = _read_instances()
    for inst in instances:
        if inst.get('pid') == pid:
            inst['hwnd'] = new_hwnd
            break
    _write_instances(instances)


def cleanup_ipc() -> None:
    """Luk server-socket og fjern denne instans fra instances.json."""
    global _server_socket, _registered_pid

    _release_primary_mutex()

    if _server_socket:
        try:
            _server_socket.close()
        except OSError:
            pass
        _server_socket = None

    if _registered_pid is not None:
        pid = _registered_pid
        _registered_pid = None
        instances = [i for i in _read_instances() if i.get('pid') != pid]
        _write_instances(instances)
