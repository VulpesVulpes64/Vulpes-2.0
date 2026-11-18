#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GameX Wire Control Center
Учебная реализация конфигуратора проводного геймпада по ТЗ проекта.

Что реализовано:
- состояния приложения INIT / DISCONNECTED / CONNECTED / EDITING / APPLYING / TESTING / ERROR;
- подключение реального геймпада через pygame (если установлен);
- режим программного симулятора без физического геймпада;
- мониторинг цифровых кнопок, D-pad, двух стиков и двух триггеров;
- переназначение цифровых кнопок внутри конфигуратора;
- dead zone, инверсия, чувствительность и кривые отклика стиков;
- dead zone и чувствительность триггеров;
- двухканальный тест вибрации через pygame.Joystick.rumble(), если устройство поддерживает;
- RGB-настройки как часть профиля и точка расширения для vendor HID-протокола;
- локальные профили JSON, импорт/экспорт, Default-профиль;
- журналирование с ротацией файлов;
- применение/отмена изменений и проверка конфигурации.

Ограничение аппаратного слоя:
стандарт HID/XInput не определяет универсальные команды для записи RGB, dead zone и
переназначений непосредственно во внутреннюю память любого геймпада. Для серийного
GameX в класс VendorHardwareAdapter необходимо добавить VID/PID и формат vendor HID
report'ов из прошивки устройства. Вся остальная архитектура уже готова к этому слою.

Необязательные зависимости для реального геймпада:
    pip install pygame

Без pygame программа запускается полностью в режиме симулятора.
"""

from __future__ import annotations

import copy
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import shutil
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, colorchooser, simpledialog

try:
    import pygame  # type: ignore
    PYGAME_AVAILABLE = True
except Exception:
    pygame = None
    PYGAME_AVAILABLE = False


APP_NAME = "GameX Wire Control Center"
APP_VERSION = "1.0.0"
POLL_MS = 16  # ~60 Гц
PROFILE_VERSION = 1

BASE_DIR = Path.home() / ".gamex_wire_control_center"
PROFILE_DIR = BASE_DIR / "profiles"
LOG_DIR = BASE_DIR / "logs"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

DIGITAL_INPUTS = [
    "A", "B", "X", "Y",
    "D-pad Up", "D-pad Down", "D-pad Left", "D-pad Right",
    "LB", "RB", "L3", "R3", "View/Back", "Menu/Start", "Home/Guide",
]

REMAPPABLE_OUTPUTS = [
    "A", "B", "X", "Y",
    "D-pad Up", "D-pad Down", "D-pad Left", "D-pad Right",
    "LB", "RB", "L3", "R3", "View/Back", "Menu/Start",
]

AXIS_NAMES = ["Left Stick X", "Left Stick Y", "Right Stick X", "Right Stick Y"]
TRIGGER_NAMES = ["LT", "RT"]
RGB_MODES = ["Выключено", "Постоянный цвет", "Дыхание", "Пульсация"]
CURVES = ["Линейная", "Плавная", "Агрессивная"]

# Типичное отображение кнопок Xbox-контроллера в pygame/SDL.
# На отдельных моделях индексы могут отличаться; поэтому есть вкладка «Диагностика».
DEFAULT_PYGAME_BUTTON_INDEX = {
    "A": 0,
    "B": 1,
    "X": 2,
    "Y": 3,
    "LB": 4,
    "RB": 5,
    "View/Back": 6,
    "Menu/Start": 7,
    "L3": 8,
    "R3": 9,
    "Home/Guide": 10,
}

# Типичная SDL/Xbox схема осей. Реальное устройство может отличаться.
DEFAULT_PYGAME_AXIS_INDEX = {
    "Left Stick X": 0,
    "Left Stick Y": 1,
    "Right Stick X": 2,
    "Right Stick Y": 3,
    "LT": 4,
    "RT": 5,
}


class AppState:
    INIT = "S0: INIT"
    DISCONNECTED = "S1: DISCONNECTED"
    CONNECTED = "S2: CONNECTED"
    EDITING = "S3: EDITING"
    APPLYING = "S4: APPLYING"
    TESTING = "S5: TESTING"
    ERROR = "S6: ERROR"


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("GameX")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_DIR / "gamex.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
    return logger


LOGGER = setup_logger()


@dataclass
class AxisSettings:
    inner_deadzone: int = 8
    outer_deadzone: int = 0
    sensitivity: int = 100
    invert: bool = False
    curve: str = "Линейная"


@dataclass
class TriggerSettings:
    inner_deadzone: int = 0
    outer_deadzone: int = 0
    sensitivity: int = 100


@dataclass
class VibrationSettings:
    left_limit: int = 100
    right_limit: int = 100


@dataclass
class RGBSettings:
    enabled: bool = True
    color: str = "#4AA3FF"
    brightness: int = 100
    mode: str = "Постоянный цвет"
    period_s: float = 1.5


@dataclass
class Profile:
    version: int = PROFILE_VERSION
    name: str = "Default"
    button_map: Dict[str, str] = field(default_factory=lambda: {
        key: key for key in DIGITAL_INPUTS if key != "Home/Guide"
    })
    axes: Dict[str, AxisSettings] = field(default_factory=lambda: {
        name: AxisSettings() for name in AXIS_NAMES
    })
    triggers: Dict[str, TriggerSettings] = field(default_factory=lambda: {
        name: TriggerSettings() for name in TRIGGER_NAMES
    })
    vibration: VibrationSettings = field(default_factory=VibrationSettings)
    rgb: RGBSettings = field(default_factory=RGBSettings)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Profile":
        validate_profile_dict(data)
        axes = {name: AxisSettings(**data["axes"][name]) for name in AXIS_NAMES}
        triggers = {name: TriggerSettings(**data["triggers"][name]) for name in TRIGGER_NAMES}
        return Profile(
            version=int(data.get("version", PROFILE_VERSION)),
            name=str(data["name"]),
            button_map=dict(data["button_map"]),
            axes=axes,
            triggers=triggers,
            vibration=VibrationSettings(**data["vibration"]),
            rgb=RGBSettings(**data["rgb"]),
        )


def validate_profile_dict(data: dict) -> None:
    required = {"version", "name", "button_map", "axes", "triggers", "vibration", "rgb"}
    if not required.issubset(data):
        missing = required - set(data)
        raise ValueError(f"В профиле отсутствуют поля: {', '.join(sorted(missing))}")

    if int(data["version"]) != PROFILE_VERSION:
        raise ValueError(f"Неподдерживаемая версия профиля: {data['version']}")

    name = str(data["name"])
    if not (1 <= len(name) <= 50):
        raise ValueError("Имя профиля должно содержать 1–50 символов")

    for key in DIGITAL_INPUTS:
        if key == "Home/Guide":
            continue
        if key not in data["button_map"]:
            raise ValueError(f"Нет назначения для кнопки {key}")
        if data["button_map"][key] not in REMAPPABLE_OUTPUTS:
            raise ValueError(f"Недопустимый логический выход для {key}")

    for axis in AXIS_NAMES:
        if axis not in data["axes"]:
            raise ValueError(f"Нет настроек оси {axis}")
        cfg = data["axes"][axis]
        if not 0 <= int(cfg["inner_deadzone"]) <= 30:
            raise ValueError(f"Внутренняя dead zone {axis}: допустимо 0–30%")
        if not 0 <= int(cfg["outer_deadzone"]) <= 30:
            raise ValueError(f"Внешняя dead zone {axis}: допустимо 0–30%")
        if not 50 <= int(cfg["sensitivity"]) <= 200:
            raise ValueError(f"Чувствительность {axis}: допустимо 50–200%")
        if cfg["curve"] not in CURVES:
            raise ValueError(f"Неизвестная кривая для {axis}")

    for trig in TRIGGER_NAMES:
        if trig not in data["triggers"]:
            raise ValueError(f"Нет настроек триггера {trig}")
        cfg = data["triggers"][trig]
        if not 0 <= int(cfg["inner_deadzone"]) <= 20:
            raise ValueError(f"Внутренняя dead zone {trig}: допустимо 0–20%")
        if not 0 <= int(cfg["outer_deadzone"]) <= 20:
            raise ValueError(f"Внешняя dead zone {trig}: допустимо 0–20%")
        if not 50 <= int(cfg["sensitivity"]) <= 200:
            raise ValueError(f"Чувствительность {trig}: допустимо 50–200%")

    vib = data["vibration"]
    if not 0 <= int(vib["left_limit"]) <= 100 or not 0 <= int(vib["right_limit"]) <= 100:
        raise ValueError("Интенсивность вибрации должна быть в диапазоне 0–100%")

    rgb = data["rgb"]
    if rgb["mode"] not in RGB_MODES:
        raise ValueError("Неизвестный режим RGB")
    if not 0 <= int(rgb["brightness"]) <= 100:
        raise ValueError("Яркость RGB должна быть 0–100%")
    if not 0.5 <= float(rgb["period_s"]) <= 5.0:
        raise ValueError("Период RGB должен быть 0.5–5.0 с")
    color = str(rgb["color"])
    if len(color) != 7 or not color.startswith("#"):
        raise ValueError("Цвет RGB должен иметь формат #RRGGBB")
    int(color[1:], 16)


class ProfileManager:
    def __init__(self) -> None:
        self.ensure_default()

    def profile_path(self, name: str) -> Path:
        safe = "".join(ch for ch in name if ch.isalnum() or ch in " _-.()").strip()
        if not safe:
            safe = "profile"
        return PROFILE_DIR / f"{safe}.json"

    def ensure_default(self) -> None:
        path = self.profile_path("Default")
        if not path.exists():
            self.save(Profile(name="Default"))

    def save(self, profile: Profile) -> Path:
        data = profile.to_dict()
        validate_profile_dict(data)
        path = self.profile_path(profile.name)
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp.replace(path)
        LOGGER.info("PROFILE | сохранён профиль '%s'", profile.name)
        return path

    def load(self, name: str) -> Profile:
        path = self.profile_path(name)
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        profile = Profile.from_dict(data)
        LOGGER.info("PROFILE | загружен профиль '%s'", name)
        return profile

    def list_names(self) -> List[str]:
        names = []
        for path in sorted(PROFILE_DIR.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if "name" in data:
                    names.append(str(data["name"]))
            except Exception:
                pass
        return sorted(set(names), key=lambda s: (s != "Default", s.lower()))

    def delete(self, name: str) -> None:
        if name == "Default":
            raise ValueError("Профиль Default защищён от удаления")
        path = self.profile_path(name)
        if path.exists():
            path.unlink()
            LOGGER.info("PROFILE | удалён профиль '%s'", name)

    def export_profile(self, name: str, dst: Path) -> None:
        src = self.profile_path(name)
        if src.stat().st_size > 1024 * 1024:
            raise ValueError("Файл профиля превышает 1 МБ")
        shutil.copy2(src, dst)
        LOGGER.info("PROFILE | экспорт '%s' -> %s", name, dst)

    def import_profile(self, src: Path) -> Profile:
        if src.stat().st_size > 1024 * 1024:
            raise ValueError("Файл профиля превышает 1 МБ")
        with src.open("r", encoding="utf-8") as f:
            data = json.load(f)
        profile = Profile.from_dict(data)
        self.save(profile)
        LOGGER.info("PROFILE | импортирован '%s'", profile.name)
        return profile


@dataclass
class ControllerSnapshot:
    buttons: Dict[str, bool] = field(default_factory=lambda: {k: False for k in DIGITAL_INPUTS})
    axes: Dict[str, float] = field(default_factory=lambda: {k: 0.0 for k in AXIS_NAMES})
    triggers: Dict[str, float] = field(default_factory=lambda: {k: 0.0 for k in TRIGGER_NAMES})
    connected: bool = False
    device_name: str = ""


class PygameBackend:
    """Чтение стандартного USB-геймпада через SDL/pygame."""

    def __init__(self) -> None:
        self.joystick = None
        self.initialized = False
        self.last_error = ""

    def initialize(self) -> None:
        if not PYGAME_AVAILABLE:
            self.last_error = "pygame не установлен"
            return
        if self.initialized:
            return
        try:
            pygame.init()
            pygame.joystick.init()
            self.initialized = True
            LOGGER.info("HID | pygame инициализирован")
        except Exception as exc:
            self.last_error = str(exc)
            LOGGER.exception("HID | ошибка инициализации pygame")

    def connect_first(self) -> bool:
        self.initialize()
        if not PYGAME_AVAILABLE or not self.initialized:
            return False
        try:
            pygame.event.pump()
            count = pygame.joystick.get_count()
            if count <= 0:
                self.joystick = None
                return False
            js = pygame.joystick.Joystick(0)
            js.init()
            self.joystick = js
            LOGGER.info(
                "HID | подключено устройство: %s; buttons=%s axes=%s hats=%s",
                js.get_name(), js.get_numbuttons(), js.get_numaxes(), js.get_numhats()
            )
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.joystick = None
            LOGGER.exception("HID | ошибка подключения")
            return False

    def disconnect(self) -> None:
        if self.joystick is not None:
            try:
                self.joystick.quit()
            except Exception:
                pass
        self.joystick = None

    def is_connected(self) -> bool:
        if self.joystick is None:
            return False
        try:
            return bool(self.joystick.get_init())
        except Exception:
            return False

    @staticmethod
    def _trigger_normalize(raw: float) -> float:
        # SDL часто отдаёт триггеры -1..+1, где -1 = отпущен.
        value = (raw + 1.0) / 2.0
        return max(0.0, min(1.0, value))

    def poll(self) -> ControllerSnapshot:
        snap = ControllerSnapshot()
        if not self.is_connected():
            return snap
        try:
            pygame.event.pump()
            js = self.joystick
            snap.connected = True
            snap.device_name = js.get_name()

            for name, idx in DEFAULT_PYGAME_BUTTON_INDEX.items():
                if idx < js.get_numbuttons():
                    snap.buttons[name] = bool(js.get_button(idx))

            if js.get_numhats() > 0:
                hx, hy = js.get_hat(0)
                snap.buttons["D-pad Up"] = hy > 0
                snap.buttons["D-pad Down"] = hy < 0
                snap.buttons["D-pad Left"] = hx < 0
                snap.buttons["D-pad Right"] = hx > 0

            for name in AXIS_NAMES:
                idx = DEFAULT_PYGAME_AXIS_INDEX[name]
                if idx < js.get_numaxes():
                    raw = float(js.get_axis(idx))
                    if name.endswith(" Y"):
                        raw = -raw
                    snap.axes[name] = max(-1.0, min(1.0, raw))

            for name in TRIGGER_NAMES:
                idx = DEFAULT_PYGAME_AXIS_INDEX[name]
                if idx < js.get_numaxes():
                    snap.triggers[name] = self._trigger_normalize(float(js.get_axis(idx)))
        except Exception as exc:
            self.last_error = str(exc)
            LOGGER.exception("HID | ошибка чтения")
            self.disconnect()
        return snap

    def rumble(self, left: float, right: float, duration_ms: int = 500) -> bool:
        if not self.is_connected():
            return False
        try:
            if not hasattr(self.joystick, "rumble"):
                return False
            return bool(self.joystick.rumble(left, right, duration_ms))
        except Exception:
            LOGGER.exception("HID | ошибка вибрации")
            return False

    def stop_rumble(self) -> None:
        if not self.is_connected():
            return
        try:
            if hasattr(self.joystick, "stop_rumble"):
                self.joystick.stop_rumble()
        except Exception:
            LOGGER.exception("HID | ошибка остановки вибрации")

    def raw_diagnostics(self) -> str:
        if not self.is_connected():
            return "Устройство не подключено."
        js = self.joystick
        try:
            pygame.event.pump()
            lines = [
                f"Устройство: {js.get_name()}",
                f"GUID: {getattr(js, 'get_guid', lambda: 'n/a')()}",
                f"Кнопок: {js.get_numbuttons()}",
                f"Осей: {js.get_numaxes()}",
                f"HAT: {js.get_numhats()}",
                "",
                "RAW BUTTONS:",
            ]
            for i in range(js.get_numbuttons()):
                lines.append(f"  button[{i}] = {js.get_button(i)}")
            lines.append("RAW AXES:")
            for i in range(js.get_numaxes()):
                lines.append(f"  axis[{i}] = {js.get_axis(i):+.4f}")
            lines.append("RAW HATS:")
            for i in range(js.get_numhats()):
                lines.append(f"  hat[{i}] = {js.get_hat(i)}")
            return "\n".join(lines)
        except Exception as exc:
            return f"Ошибка диагностики: {exc}"


class VendorHardwareAdapter:
    """
    Точка подключения фирменного протокола GameX.

    Стандартный HID не задаёт универсальные report'ы для RGB, записи dead zone и
    постоянного remap в прошивку. Когда будут известны VID/PID и формат feature/output
    report'ов, именно здесь добавляются команды записи в реальное устройство.
    """

    def apply_profile(self, profile: Profile) -> Tuple[bool, str]:
        # Сейчас профиль считается применённым на уровне конфигуратора.
        # Возвращаем понятный статус, не имитируя неизвестный аппаратный протокол.
        return True, "Настройки применены в конфигураторе; аппаратная запись требует vendor HID-протокола GameX."

    def apply_rgb(self, rgb: RGBSettings) -> Tuple[bool, str]:
        return True, "RGB сохранён в профиле; команда на физическую подсветку требует vendor HID-протокола GameX."


class GameXApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1180x780")
        self.minsize(1040, 700)

        self.profile_manager = ProfileManager()
        self.backend = PygameBackend()
        self.vendor_adapter = VendorHardwareAdapter()

        self.state = AppState.INIT
        self.connected_real = False
        self.simulation_mode = False
        self.snapshot = ControllerSnapshot()
        self.sim_snapshot = ControllerSnapshot(connected=True, device_name="GameX Simulator")

        self.current_profile = self.profile_manager.load("Default")
        self.working_profile = copy.deepcopy(self.current_profile)
        self.dirty = False

        self.button_widgets: Dict[str, ttk.Label] = {}
        self.button_map_vars: Dict[str, tk.StringVar] = {}
        self.axis_ui = {}
        self.trigger_ui = {}
        self.sim_button_vars: Dict[str, tk.BooleanVar] = {}
        self.sim_axis_vars: Dict[str, tk.DoubleVar] = {}
        self.sim_trigger_vars: Dict[str, tk.DoubleVar] = {}

        self.status_var = tk.StringVar(value="Инициализация…")
        self.state_var = tk.StringVar(value=self.state)
        self.device_var = tk.StringVar(value="Нет устройства")
        self.dirty_var = tk.StringVar(value="")

        self._build_ui()
        self._load_profile_to_ui(self.working_profile)
        self._set_state(AppState.DISCONNECTED, "Готово. Подключите геймпад или включите симулятор.")
        LOGGER.info("APP | запуск версии %s", APP_VERSION)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.auto_detect)
        self.after(POLL_MS, self.poll_loop)
        self.after(500, self.refresh_log_view)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=8)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(4, weight=1)

        ttk.Label(top, text=APP_NAME, font=("Segoe UI", 15, "bold")).grid(row=0, column=0, padx=(0, 16))
        ttk.Label(top, textvariable=self.device_var).grid(row=0, column=1, padx=5)
        ttk.Label(top, textvariable=self.state_var).grid(row=0, column=2, padx=10)
        ttk.Label(top, textvariable=self.dirty_var).grid(row=0, column=3, padx=10)

        ttk.Button(top, text="Найти геймпад", command=self.auto_detect).grid(row=0, column=5, padx=4)
        ttk.Button(top, text="Симулятор", command=self.toggle_simulation).grid(row=0, column=6, padx=4)
        ttk.Button(top, text="Применить", command=self.apply_changes).grid(row=0, column=7, padx=4)
        ttk.Button(top, text="Отменить", command=self.cancel_changes).grid(row=0, column=8, padx=4)

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        self.tab_monitor = ttk.Frame(self.notebook, padding=10)
        self.tab_buttons = ttk.Frame(self.notebook, padding=10)
        self.tab_axes = ttk.Frame(self.notebook, padding=10)
        self.tab_vibration = ttk.Frame(self.notebook, padding=10)
        self.tab_rgb = ttk.Frame(self.notebook, padding=10)
        self.tab_profiles = ttk.Frame(self.notebook, padding=10)
        self.tab_diag = ttk.Frame(self.notebook, padding=10)
        self.tab_log = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(self.tab_monitor, text="Мониторинг")
        self.notebook.add(self.tab_buttons, text="Кнопки")
        self.notebook.add(self.tab_axes, text="Стики и триггеры")
        self.notebook.add(self.tab_vibration, text="Вибрация")
        self.notebook.add(self.tab_rgb, text="RGB")
        self.notebook.add(self.tab_profiles, text="Профили")
        self.notebook.add(self.tab_diag, text="Диагностика")
        self.notebook.add(self.tab_log, text="Журнал")

        self._build_monitor_tab()
        self._build_buttons_tab()
        self._build_axes_tab()
        self._build_vibration_tab()
        self._build_rgb_tab()
        self._build_profiles_tab()
        self._build_diag_tab()
        self._build_log_tab()

        bottom = ttk.Frame(self, padding=(8, 0, 8, 8))
        bottom.grid(row=2, column=0, sticky="ew")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left")

    def _build_monitor_tab(self) -> None:
        self.tab_monitor.columnconfigure(0, weight=1)
        self.tab_monitor.columnconfigure(1, weight=1)

        buttons_box = ttk.LabelFrame(self.tab_monitor, text="Цифровые элементы", padding=10)
        buttons_box.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        for i, name in enumerate(DIGITAL_INPUTS):
            lbl = ttk.Label(buttons_box, text=f"{name}: отпущена", width=24)
            lbl.grid(row=i // 3, column=i % 3, sticky="w", padx=6, pady=5)
            self.button_widgets[name] = lbl

        axes_box = ttk.LabelFrame(self.tab_monitor, text="Аналоговые каналы", padding=10)
        axes_box.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 6))
        self.monitor_axis_vars: Dict[str, tk.StringVar] = {}
        for i, name in enumerate(AXIS_NAMES + TRIGGER_NAMES):
            var = tk.StringVar(value="0.0%")
            self.monitor_axis_vars[name] = var
            ttk.Label(axes_box, text=name, width=18).grid(row=i, column=0, sticky="w", pady=4)
            ttk.Label(axes_box, textvariable=var, width=12).grid(row=i, column=1, sticky="w", pady=4)

        sim_box = ttk.LabelFrame(self.tab_monitor, text="Симулятор входов", padding=10)
        sim_box.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(sim_box, text="Работает только при включённом режиме «Симулятор».").grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 8))
        for i, name in enumerate(DIGITAL_INPUTS):
            var = tk.BooleanVar(value=False)
            self.sim_button_vars[name] = var
            cb = ttk.Checkbutton(sim_box, text=name, variable=var)
            cb.grid(row=1 + i // 5, column=i % 5, sticky="w", padx=5, pady=3)

        start_row = 1 + math.ceil(len(DIGITAL_INPUTS) / 5)
        for i, name in enumerate(AXIS_NAMES):
            var = tk.DoubleVar(value=0.0)
            self.sim_axis_vars[name] = var
            ttk.Label(sim_box, text=name).grid(row=start_row + i, column=0, sticky="w")
            ttk.Scale(sim_box, from_=-1.0, to=1.0, variable=var, orient="horizontal", length=320).grid(row=start_row + i, column=1, columnspan=2, sticky="ew", pady=3)
        for i, name in enumerate(TRIGGER_NAMES):
            var = tk.DoubleVar(value=0.0)
            self.sim_trigger_vars[name] = var
            ttk.Label(sim_box, text=name).grid(row=start_row + i, column=3, sticky="w")
            ttk.Scale(sim_box, from_=0.0, to=1.0, variable=var, orient="horizontal", length=250).grid(row=start_row + i, column=4, columnspan=2, sticky="ew", pady=3)

    def _build_buttons_tab(self) -> None:
        self.tab_buttons.columnconfigure(1, weight=1)
        ttk.Label(
            self.tab_buttons,
            text="Физический элемент → логический выход. Home/Guide сохраняет системное назначение.",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        row = 1
        for name in DIGITAL_INPUTS:
            ttk.Label(self.tab_buttons, text=name, width=22).grid(row=row, column=0, sticky="w", pady=4)
            if name == "Home/Guide":
                ttk.Label(self.tab_buttons, text="Системный Guide/Home (не переназначается)").grid(row=row, column=1, sticky="w")
            else:
                var = tk.StringVar(value=name)
                self.button_map_vars[name] = var
                combo = ttk.Combobox(self.tab_buttons, textvariable=var, values=REMAPPABLE_OUTPUTS, state="readonly", width=28)
                combo.grid(row=row, column=1, sticky="w")
                combo.bind("<<ComboboxSelected>>", lambda _e: self.mark_dirty())
            row += 1

        ttk.Button(self.tab_buttons, text="Сбросить назначения", command=self.reset_button_mapping).grid(row=row, column=0, pady=12, sticky="w")

    def _build_axes_tab(self) -> None:
        canvas = tk.Canvas(self.tab_axes, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.tab_axes, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Label(inner, text="Обработка оси: raw → dead zone → инверсия → кривая → чувствительность → ограничение").grid(row=0, column=0, columnspan=7, sticky="w", pady=(0, 10))

        row = 1
        for name in AXIS_NAMES:
            box = ttk.LabelFrame(inner, text=name, padding=8)
            box.grid(row=row, column=0, sticky="ew", pady=5)
            box.columnconfigure(8, weight=1)

            inner_var = tk.IntVar(value=8)
            outer_var = tk.IntVar(value=0)
            sens_var = tk.IntVar(value=100)
            invert_var = tk.BooleanVar(value=False)
            curve_var = tk.StringVar(value="Линейная")
            raw_var = tk.StringVar(value="raw: 0.0%")
            out_var = tk.StringVar(value="out: 0.0%")
            self.axis_ui[name] = {
                "inner": inner_var, "outer": outer_var, "sens": sens_var,
                "invert": invert_var, "curve": curve_var,
                "raw": raw_var, "out": out_var,
            }

            ttk.Label(box, text="Dead zone внутр., %").grid(row=0, column=0, padx=4)
            ttk.Spinbox(box, from_=0, to=30, textvariable=inner_var, width=6, command=self.mark_dirty).grid(row=0, column=1)
            ttk.Label(box, text="внеш., %").grid(row=0, column=2, padx=4)
            ttk.Spinbox(box, from_=0, to=30, textvariable=outer_var, width=6, command=self.mark_dirty).grid(row=0, column=3)
            ttk.Label(box, text="Чувств., %").grid(row=0, column=4, padx=4)
            ttk.Spinbox(box, from_=50, to=200, textvariable=sens_var, width=6, command=self.mark_dirty).grid(row=0, column=5)
            ttk.Checkbutton(box, text="Инверсия", variable=invert_var, command=self.mark_dirty).grid(row=0, column=6, padx=6)
            combo = ttk.Combobox(box, textvariable=curve_var, values=CURVES, state="readonly", width=15)
            combo.grid(row=0, column=7, padx=5)
            combo.bind("<<ComboboxSelected>>", lambda _e: self.mark_dirty())
            ttk.Label(box, textvariable=raw_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
            ttk.Label(box, textvariable=out_var).grid(row=1, column=2, columnspan=2, sticky="w", pady=(5, 0))
            row += 1

        for name in TRIGGER_NAMES:
            box = ttk.LabelFrame(inner, text=name, padding=8)
            box.grid(row=row, column=0, sticky="ew", pady=5)
            inner_var = tk.IntVar(value=0)
            outer_var = tk.IntVar(value=0)
            sens_var = tk.IntVar(value=100)
            raw_var = tk.StringVar(value="raw: 0.0%")
            out_var = tk.StringVar(value="out: 0.0%")
            self.trigger_ui[name] = {"inner": inner_var, "outer": outer_var, "sens": sens_var, "raw": raw_var, "out": out_var}
            ttk.Label(box, text="Dead zone внутр., %").grid(row=0, column=0, padx=4)
            ttk.Spinbox(box, from_=0, to=20, textvariable=inner_var, width=6, command=self.mark_dirty).grid(row=0, column=1)
            ttk.Label(box, text="внеш., %").grid(row=0, column=2, padx=4)
            ttk.Spinbox(box, from_=0, to=20, textvariable=outer_var, width=6, command=self.mark_dirty).grid(row=0, column=3)
            ttk.Label(box, text="Чувств., %").grid(row=0, column=4, padx=4)
            ttk.Spinbox(box, from_=50, to=200, textvariable=sens_var, width=6, command=self.mark_dirty).grid(row=0, column=5)
            ttk.Label(box, textvariable=raw_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
            ttk.Label(box, textvariable=out_var).grid(row=1, column=2, columnspan=2, sticky="w", pady=(5, 0))
            row += 1

    def _build_vibration_tab(self) -> None:
        self.vib_left_var = tk.IntVar(value=100)
        self.vib_right_var = tk.IntVar(value=100)

        ttk.Label(self.tab_vibration, text="Левый низкочастотный мотор, %").grid(row=0, column=0, sticky="w", pady=8)
        ttk.Scale(self.tab_vibration, from_=0, to=100, variable=self.vib_left_var, orient="horizontal", length=420, command=lambda _v: self.mark_dirty()).grid(row=0, column=1, sticky="w")
        ttk.Label(self.tab_vibration, text="Правый высокочастотный мотор, %").grid(row=1, column=0, sticky="w", pady=8)
        ttk.Scale(self.tab_vibration, from_=0, to=100, variable=self.vib_right_var, orient="horizontal", length=420, command=lambda _v: self.mark_dirty()).grid(row=1, column=1, sticky="w")

        buttons = ttk.Frame(self.tab_vibration)
        buttons.grid(row=2, column=0, columnspan=2, sticky="w", pady=15)
        ttk.Button(buttons, text="Тест левого 500 мс", command=lambda: self.test_rumble("left")).pack(side="left", padx=4)
        ttk.Button(buttons, text="Тест правого 500 мс", command=lambda: self.test_rumble("right")).pack(side="left", padx=4)
        ttk.Button(buttons, text="Тест обоих 500 мс", command=lambda: self.test_rumble("both")).pack(side="left", padx=4)
        ttk.Button(buttons, text="СТОП", command=self.stop_rumble).pack(side="left", padx=4)

        ttk.Label(
            self.tab_vibration,
            text="Для реального устройства тест использует pygame.Joystick.rumble(), если драйвер его поддерживает.\nВ режиме симулятора выполняется программная имитация состояния TESTING.",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=8)

    def _build_rgb_tab(self) -> None:
        self.rgb_enabled_var = tk.BooleanVar(value=True)
        self.rgb_color_var = tk.StringVar(value="#4AA3FF")
        self.rgb_brightness_var = tk.IntVar(value=100)
        self.rgb_mode_var = tk.StringVar(value="Постоянный цвет")
        self.rgb_period_var = tk.DoubleVar(value=1.5)

        ttk.Checkbutton(self.tab_rgb, text="Подсветка включена", variable=self.rgb_enabled_var, command=self.mark_dirty).grid(row=0, column=0, sticky="w", pady=7)
        ttk.Label(self.tab_rgb, text="Цвет").grid(row=1, column=0, sticky="w")
        ttk.Entry(self.tab_rgb, textvariable=self.rgb_color_var, width=12).grid(row=1, column=1, sticky="w")
        ttk.Button(self.tab_rgb, text="Выбрать…", command=self.choose_rgb_color).grid(row=1, column=2, padx=5)
        self.rgb_preview = tk.Label(self.tab_rgb, width=10, bg="#4AA3FF", relief="sunken")
        self.rgb_preview.grid(row=1, column=3, padx=8)

        ttk.Label(self.tab_rgb, text="Яркость, %").grid(row=2, column=0, sticky="w", pady=7)
        ttk.Scale(self.tab_rgb, from_=0, to=100, variable=self.rgb_brightness_var, orient="horizontal", length=350, command=lambda _v: self.mark_dirty()).grid(row=2, column=1, columnspan=2, sticky="w")
        ttk.Label(self.tab_rgb, text="Режим").grid(row=3, column=0, sticky="w", pady=7)
        combo = ttk.Combobox(self.tab_rgb, textvariable=self.rgb_mode_var, values=RGB_MODES, state="readonly", width=22)
        combo.grid(row=3, column=1, sticky="w")
        combo.bind("<<ComboboxSelected>>", lambda _e: self.mark_dirty())
        ttk.Label(self.tab_rgb, text="Период эффекта, с").grid(row=4, column=0, sticky="w", pady=7)
        ttk.Spinbox(self.tab_rgb, from_=0.5, to=5.0, increment=0.1, textvariable=self.rgb_period_var, width=8, command=self.mark_dirty).grid(row=4, column=1, sticky="w")

        ttk.Label(
            self.tab_rgb,
            text="Параметры RGB полностью сохраняются в профиль. Физическая запись цвета в конкретный GameX\nподключается через VendorHardwareAdapter после получения формата HID-команд прошивки.",
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=15)

    def _build_profiles_tab(self) -> None:
        self.tab_profiles.columnconfigure(0, weight=1)
        self.tab_profiles.rowconfigure(1, weight=1)

        ttk.Label(self.tab_profiles, text="Локальные профили").grid(row=0, column=0, sticky="w")
        self.profile_list = tk.Listbox(self.tab_profiles, height=18)
        self.profile_list.grid(row=1, column=0, sticky="nsew", pady=5)

        buttons = ttk.Frame(self.tab_profiles)
        buttons.grid(row=1, column=1, sticky="ns", padx=10)
        ttk.Button(buttons, text="Обновить", command=self.refresh_profile_list).pack(fill="x", pady=3)
        ttk.Button(buttons, text="Загрузить", command=self.load_selected_profile).pack(fill="x", pady=3)
        ttk.Button(buttons, text="Сохранить", command=self.save_current_profile).pack(fill="x", pady=3)
        ttk.Button(buttons, text="Сохранить как…", command=self.save_profile_as).pack(fill="x", pady=3)
        ttk.Button(buttons, text="Удалить", command=self.delete_selected_profile).pack(fill="x", pady=3)
        ttk.Separator(buttons).pack(fill="x", pady=8)
        ttk.Button(buttons, text="Импорт…", command=self.import_profile).pack(fill="x", pady=3)
        ttk.Button(buttons, text="Экспорт…", command=self.export_selected_profile).pack(fill="x", pady=3)
        ttk.Button(buttons, text="Заводской Default", command=self.load_default_profile).pack(fill="x", pady=3)
        self.refresh_profile_list()

    def _build_diag_tab(self) -> None:
        top = ttk.Frame(self.tab_diag)
        top.pack(fill="x", pady=(0, 6))
        ttk.Button(top, text="Обновить RAW", command=self.refresh_diagnostics).pack(side="left", padx=3)
        ttk.Button(top, text="Копировать", command=self.copy_diagnostics).pack(side="left", padx=3)
        self.diag_text = tk.Text(self.tab_diag, wrap="none", height=30)
        self.diag_text.pack(fill="both", expand=True)
        self.diag_text.insert("1.0", "Диагностика покажет индексы кнопок/осей реального устройства.\n")

    def _build_log_tab(self) -> None:
        self.log_text = tk.Text(self.tab_log, wrap="none", height=30)
        self.log_text.pack(fill="both", expand=True)

    # ---------- State ----------
    def _set_state(self, state: str, status: str = "") -> None:
        self.state = state
        self.state_var.set(state)
        if status:
            self.status_var.set(status)
        LOGGER.info("STATE | %s | %s", state, status)

    def mark_dirty(self) -> None:
        self.dirty = True
        self.dirty_var.set("● есть неподтверждённые изменения")
        if self.state in (AppState.CONNECTED, AppState.DISCONNECTED):
            self._set_state(AppState.EDITING, "Изменения ещё не применены.")

    # ---------- Connection ----------
    def auto_detect(self) -> None:
        if self.simulation_mode:
            self.device_var.set("GameX Simulator")
            self._set_state(AppState.CONNECTED, "Симулятор подключён.")
            return

        if self.backend.connect_first():
            self.connected_real = True
            name = self.backend.joystick.get_name() if self.backend.joystick else "USB Gamepad"
            self.device_var.set(name)
            self._set_state(AppState.CONNECTED, "Геймпад обнаружен.")
        else:
            self.connected_real = False
            self.device_var.set("Нет устройства")
            self._set_state(AppState.DISCONNECTED, "Геймпад не найден. Можно включить симулятор.")

    def toggle_simulation(self) -> None:
        self.simulation_mode = not self.simulation_mode
        if self.simulation_mode:
            self.backend.disconnect()
            self.connected_real = False
            self.device_var.set("GameX Simulator")
            self._set_state(AppState.CONNECTED, "Режим симулятора включён.")
            LOGGER.info("SIM | включён")
        else:
            self.device_var.set("Нет устройства")
            self._set_state(AppState.DISCONNECTED, "Режим симулятора выключен.")
            LOGGER.info("SIM | выключен")
            self.auto_detect()

    def _build_sim_snapshot(self) -> ControllerSnapshot:
        snap = ControllerSnapshot(connected=True, device_name="GameX Simulator")
        for name, var in self.sim_button_vars.items():
            snap.buttons[name] = bool(var.get())
        for name, var in self.sim_axis_vars.items():
            snap.axes[name] = float(var.get())
        for name, var in self.sim_trigger_vars.items():
            snap.triggers[name] = float(var.get())
        return snap

    # ---------- Processing ----------
    @staticmethod
    def process_axis(raw: float, cfg: AxisSettings) -> float:
        x = max(-1.0, min(1.0, raw))
        sign = -1.0 if x < 0 else 1.0
        a = abs(x)
        inner = cfg.inner_deadzone / 100.0
        outer = cfg.outer_deadzone / 100.0
        usable_end = max(inner + 1e-6, 1.0 - outer)

        if a <= inner:
            y = 0.0
        elif a >= usable_end:
            y = 1.0
        else:
            y = (a - inner) / (usable_end - inner)

        if cfg.curve == "Плавная":
            y = y * y
        elif cfg.curve == "Агрессивная":
            y = math.sqrt(y)

        y *= cfg.sensitivity / 100.0
        y = min(1.0, y)
        y *= sign
        if cfg.invert:
            y = -y
        return max(-1.0, min(1.0, y))

    @staticmethod
    def process_trigger(raw: float, cfg: TriggerSettings) -> float:
        x = max(0.0, min(1.0, raw))
        inner = cfg.inner_deadzone / 100.0
        outer = cfg.outer_deadzone / 100.0
        usable_end = max(inner + 1e-6, 1.0 - outer)
        if x <= inner:
            y = 0.0
        elif x >= usable_end:
            y = 1.0
        else:
            y = (x - inner) / (usable_end - inner)
        y *= cfg.sensitivity / 100.0
        return max(0.0, min(1.0, y))

    def get_processed_state(self) -> Tuple[Dict[str, bool], Dict[str, float], Dict[str, float]]:
        # При каждом цикле берём настройки из текущих UI-контролов, чтобы диагностика
        # сразу показывала эффект до нажатия «Применить».
        try:
            work = self._profile_from_ui(self.current_profile.name)
        except Exception:
            work = self.working_profile

        logical_buttons = {key: False for key in REMAPPABLE_OUTPUTS}
        for physical, pressed in self.snapshot.buttons.items():
            if not pressed or physical == "Home/Guide":
                continue
            logical = work.button_map.get(physical, physical)
            if logical in logical_buttons:
                logical_buttons[logical] = True

        processed_axes = {
            name: self.process_axis(self.snapshot.axes[name], work.axes[name]) for name in AXIS_NAMES
        }
        processed_triggers = {
            name: self.process_trigger(self.snapshot.triggers[name], work.triggers[name]) for name in TRIGGER_NAMES
        }
        return logical_buttons, processed_axes, processed_triggers

    # ---------- Poll ----------
    def poll_loop(self) -> None:
        try:
            if self.simulation_mode:
                self.snapshot = self._build_sim_snapshot()
            elif self.connected_real:
                snap = self.backend.poll()
                if snap.connected:
                    self.snapshot = snap
                else:
                    if self.connected_real:
                        LOGGER.warning("HID | устройство отключено")
                    self.connected_real = False
                    self.snapshot = ControllerSnapshot()
                    self.device_var.set("Нет устройства")
                    if self.state != AppState.ERROR:
                        self._set_state(AppState.DISCONNECTED, "Устройство отключено.")
            else:
                self.snapshot = ControllerSnapshot()

            self.update_monitor_ui()
        except Exception as exc:
            LOGGER.exception("POLL | ошибка")
            self._set_state(AppState.ERROR, f"Ошибка цикла опроса: {exc}")
        finally:
            self.after(POLL_MS, self.poll_loop)

    def update_monitor_ui(self) -> None:
        for name, lbl in self.button_widgets.items():
            pressed = self.snapshot.buttons.get(name, False)
            lbl.configure(text=f"{name}: {'НАЖАТА' if pressed else 'отпущена'}")

        try:
            _buttons, axes, triggers = self.get_processed_state()
        except Exception:
            axes = self.snapshot.axes
            triggers = self.snapshot.triggers

        for name in AXIS_NAMES:
            raw = self.snapshot.axes.get(name, 0.0)
            out = axes.get(name, 0.0)
            self.monitor_axis_vars[name].set(f"{out * 100:+.1f}%")
            if name in self.axis_ui:
                self.axis_ui[name]["raw"].set(f"raw: {raw * 100:+.1f}%")
                self.axis_ui[name]["out"].set(f"out: {out * 100:+.1f}%")

        for name in TRIGGER_NAMES:
            raw = self.snapshot.triggers.get(name, 0.0)
            out = triggers.get(name, 0.0)
            self.monitor_axis_vars[name].set(f"{out * 100:.1f}%")
            if name in self.trigger_ui:
                self.trigger_ui[name]["raw"].set(f"raw: {raw * 100:.1f}%")
                self.trigger_ui[name]["out"].set(f"out: {out * 100:.1f}%")

    # ---------- Profile <-> UI ----------
    def _profile_from_ui(self, name: str) -> Profile:
        button_map = {k: v.get() for k, v in self.button_map_vars.items()}
        axes = {}
        for axis_name, ui in self.axis_ui.items():
            axes[axis_name] = AxisSettings(
                inner_deadzone=int(ui["inner"].get()),
                outer_deadzone=int(ui["outer"].get()),
                sensitivity=int(ui["sens"].get()),
                invert=bool(ui["invert"].get()),
                curve=str(ui["curve"].get()),
            )
        triggers = {}
        for trig_name, ui in self.trigger_ui.items():
            triggers[trig_name] = TriggerSettings(
                inner_deadzone=int(ui["inner"].get()),
                outer_deadzone=int(ui["outer"].get()),
                sensitivity=int(ui["sens"].get()),
            )
        profile = Profile(
            name=name,
            button_map=button_map,
            axes=axes,
            triggers=triggers,
            vibration=VibrationSettings(
                left_limit=int(round(self.vib_left_var.get())),
                right_limit=int(round(self.vib_right_var.get())),
            ),
            rgb=RGBSettings(
                enabled=bool(self.rgb_enabled_var.get()),
                color=str(self.rgb_color_var.get()),
                brightness=int(round(self.rgb_brightness_var.get())),
                mode=str(self.rgb_mode_var.get()),
                period_s=round(float(self.rgb_period_var.get()), 1),
            ),
        )
        validate_profile_dict(profile.to_dict())
        return profile

    def _load_profile_to_ui(self, profile: Profile) -> None:
        for key, var in self.button_map_vars.items():
            var.set(profile.button_map.get(key, key))
        for name, cfg in profile.axes.items():
            ui = self.axis_ui[name]
            ui["inner"].set(cfg.inner_deadzone)
            ui["outer"].set(cfg.outer_deadzone)
            ui["sens"].set(cfg.sensitivity)
            ui["invert"].set(cfg.invert)
            ui["curve"].set(cfg.curve)
        for name, cfg in profile.triggers.items():
            ui = self.trigger_ui[name]
            ui["inner"].set(cfg.inner_deadzone)
            ui["outer"].set(cfg.outer_deadzone)
            ui["sens"].set(cfg.sensitivity)
        self.vib_left_var.set(profile.vibration.left_limit)
        self.vib_right_var.set(profile.vibration.right_limit)
        self.rgb_enabled_var.set(profile.rgb.enabled)
        self.rgb_color_var.set(profile.rgb.color)
        self.rgb_brightness_var.set(profile.rgb.brightness)
        self.rgb_mode_var.set(profile.rgb.mode)
        self.rgb_period_var.set(profile.rgb.period_s)
        self._update_rgb_preview()
        self.dirty = False
        self.dirty_var.set("")

    def apply_changes(self) -> None:
        self._set_state(AppState.APPLYING, "Проверка и применение конфигурации…")
        start = time.perf_counter()
        try:
            profile = self._profile_from_ui(self.current_profile.name)
            ok, hw_message = self.vendor_adapter.apply_profile(profile)
            if not ok:
                raise RuntimeError(hw_message)
            self.current_profile = copy.deepcopy(profile)
            self.working_profile = copy.deepcopy(profile)
            self.dirty = False
            self.dirty_var.set("")
            elapsed = time.perf_counter() - start
            LOGGER.info("APPLY | конфигурация применена за %.3f с", elapsed)
            target_state = AppState.CONNECTED if self.snapshot.connected else AppState.DISCONNECTED
            self._set_state(target_state, f"Настройки применены за {elapsed:.2f} с. {hw_message}")
        except Exception as exc:
            LOGGER.exception("APPLY | ошибка")
            self._set_state(AppState.ERROR, f"Не удалось применить настройки: {exc}")
            messagebox.showerror(APP_NAME, str(exc))

    def cancel_changes(self) -> None:
        self.working_profile = copy.deepcopy(self.current_profile)
        self._load_profile_to_ui(self.working_profile)
        target_state = AppState.CONNECTED if self.snapshot.connected else AppState.DISCONNECTED
        self._set_state(target_state, "Неподтверждённые изменения отменены.")

    def reset_button_mapping(self) -> None:
        for name, var in self.button_map_vars.items():
            var.set(name)
        self.mark_dirty()

    # ---------- RGB ----------
    def choose_rgb_color(self) -> None:
        result = colorchooser.askcolor(color=self.rgb_color_var.get(), title="Цвет RGB")
        if result and result[1]:
            self.rgb_color_var.set(result[1].upper())
            self._update_rgb_preview()
            self.mark_dirty()

    def _update_rgb_preview(self) -> None:
        try:
            self.rgb_preview.configure(bg=self.rgb_color_var.get())
        except Exception:
            pass

    # ---------- Vibration ----------
    def test_rumble(self, mode: str) -> None:
        left = self.vib_left_var.get() / 100.0 if mode in ("left", "both") else 0.0
        right = self.vib_right_var.get() / 100.0 if mode in ("right", "both") else 0.0
        self._set_state(AppState.TESTING, f"Тест вибрации: {mode}, 500 мс")
        LOGGER.info("RUMBLE | test %s left=%.2f right=%.2f", mode, left, right)

        if self.simulation_mode:
            self.after(500, self._finish_test)
            return

        if not self.connected_real:
            messagebox.showwarning(APP_NAME, "Реальный геймпад не подключён. Включите симулятор или подключите устройство.")
            self._finish_test()
            return

        ok = self.backend.rumble(left, right, 500)
        if not ok:
            messagebox.showwarning(APP_NAME, "Драйвер/устройство не поддерживает вибрацию через pygame.")
        self.after(500, self._finish_test)

    def stop_rumble(self) -> None:
        started = time.perf_counter()
        self.backend.stop_rumble()
        elapsed_ms = (time.perf_counter() - started) * 1000
        LOGGER.info("RUMBLE | stop %.1f ms", elapsed_ms)
        self._finish_test()

    def _finish_test(self) -> None:
        target_state = AppState.EDITING if self.dirty else (AppState.CONNECTED if self.snapshot.connected else AppState.DISCONNECTED)
        self._set_state(target_state, "Тест вибрации завершён.")

    # ---------- Profiles ----------
    def refresh_profile_list(self) -> None:
        if not hasattr(self, "profile_list"):
            return
        self.profile_list.delete(0, "end")
        for name in self.profile_manager.list_names():
            self.profile_list.insert("end", name)

    def _selected_profile_name(self) -> Optional[str]:
        sel = self.profile_list.curselection()
        if not sel:
            return None
        return str(self.profile_list.get(sel[0]))

    def load_selected_profile(self) -> None:
        name = self._selected_profile_name()
        if not name:
            messagebox.showinfo(APP_NAME, "Выберите профиль.")
            return
        try:
            profile = self.profile_manager.load(name)
            self.current_profile = copy.deepcopy(profile)
            self.working_profile = copy.deepcopy(profile)
            self._load_profile_to_ui(profile)
            self.status_var.set(f"Профиль '{name}' загружен.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Ошибка загрузки профиля: {exc}")

    def save_current_profile(self) -> None:
        try:
            profile = self._profile_from_ui(self.current_profile.name)
            self.profile_manager.save(profile)
            self.current_profile = copy.deepcopy(profile)
            self.working_profile = copy.deepcopy(profile)
            self.dirty = False
            self.dirty_var.set("")
            self.refresh_profile_list()
            self.status_var.set(f"Профиль '{profile.name}' сохранён.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Ошибка сохранения: {exc}")

    def save_profile_as(self) -> None:
        name = simpledialog.askstring(APP_NAME, "Имя нового профиля (1–50 символов):")
        if not name:
            return
        name = name.strip()
        try:
            profile = self._profile_from_ui(name)
            self.profile_manager.save(profile)
            self.current_profile = copy.deepcopy(profile)
            self.working_profile = copy.deepcopy(profile)
            self._load_profile_to_ui(profile)
            self.refresh_profile_list()
            self.status_var.set(f"Создан профиль '{name}'.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Ошибка: {exc}")

    def delete_selected_profile(self) -> None:
        name = self._selected_profile_name()
        if not name:
            return
        if not messagebox.askyesno(APP_NAME, f"Удалить профиль '{name}'?"):
            return
        try:
            self.profile_manager.delete(name)
            self.refresh_profile_list()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def load_default_profile(self) -> None:
        try:
            profile = self.profile_manager.load("Default")
            self.current_profile = copy.deepcopy(profile)
            self.working_profile = copy.deepcopy(profile)
            self._load_profile_to_ui(profile)
            self.status_var.set("Восстановлен заводской профиль Default.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def import_profile(self) -> None:
        path = filedialog.askopenfilename(title="Импорт профиля", filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")])
        if not path:
            return
        try:
            profile = self.profile_manager.import_profile(Path(path))
            self.refresh_profile_list()
            self.current_profile = copy.deepcopy(profile)
            self.working_profile = copy.deepcopy(profile)
            self._load_profile_to_ui(profile)
            self.status_var.set(f"Импортирован профиль '{profile.name}'.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Профиль отклонён целиком: {exc}")

    def export_selected_profile(self) -> None:
        name = self._selected_profile_name()
        if not name:
            messagebox.showinfo(APP_NAME, "Выберите профиль.")
            return
        path = filedialog.asksaveasfilename(
            title="Экспорт профиля",
            defaultextension=".json",
            initialfile=f"{name}.json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        try:
            self.profile_manager.export_profile(name, Path(path))
            self.status_var.set(f"Профиль '{name}' экспортирован.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    # ---------- Diagnostics / logs ----------
    def refresh_diagnostics(self) -> None:
        if self.simulation_mode:
            text = "GameX Simulator\n\n"
            text += "DIGITAL:\n" + "\n".join(f"  {k}: {int(v.get())}" for k, v in self.sim_button_vars.items())
            text += "\n\nAXES:\n" + "\n".join(f"  {k}: {v.get():+.4f}" for k, v in self.sim_axis_vars.items())
            text += "\n\nTRIGGERS:\n" + "\n".join(f"  {k}: {v.get():.4f}" for k, v in self.sim_trigger_vars.items())
        elif self.connected_real:
            text = self.backend.raw_diagnostics()
        else:
            text = "Устройство не подключено.\n\nУстановите pygame: pip install pygame\nили включите программный симулятор."
        self.diag_text.delete("1.0", "end")
        self.diag_text.insert("1.0", text)

    def copy_diagnostics(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.diag_text.get("1.0", "end-1c"))
        self.status_var.set("Диагностика скопирована в буфер обмена.")

    def refresh_log_view(self) -> None:
        try:
            log_path = LOG_DIR / "gamex.log"
            if log_path.exists():
                text = log_path.read_text(encoding="utf-8", errors="replace")
                if len(text) > 50000:
                    text = text[-50000:]
                self.log_text.delete("1.0", "end")
                self.log_text.insert("1.0", text)
                self.log_text.see("end")
        except Exception:
            pass
        self.after(1000, self.refresh_log_view)

    # ---------- Closing ----------
    def on_close(self) -> None:
        if self.dirty:
            choice = messagebox.askyesnocancel(APP_NAME, "Есть неподтверждённые изменения. Сохранить профиль перед выходом?")
            if choice is None:
                return
            if choice:
                self.save_current_profile()
        try:
            self.backend.stop_rumble()
            self.backend.disconnect()
            if PYGAME_AVAILABLE:
                pygame.quit()
        except Exception:
            pass
        LOGGER.info("APP | завершение")
        self.destroy()


def main() -> int:
    try:
        app = GameXApp()
        app.mainloop()
        return 0
    except tk.TclError as exc:
        print(f"Ошибка Tkinter: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        LOGGER.exception("FATAL | необработанная ошибка")
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
