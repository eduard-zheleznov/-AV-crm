from __future__ import annotations

import queue

from avito_crm import gui


class Button:
    def __init__(self):
        self.values = {}

    def configure(self, **values):
        self.values.update(values)


class Root:
    def winfo_exists(self):
        return False


def app(tmp_path):
    instance = gui.DesktopApp.__new__(gui.DesktopApp)
    instance.project_root = tmp_path
    instance.diagnostics_button = Button()
    instance.root = Root()
    instance.events = queue.Queue()
    instance.process = object()  # An unrelated running queue must remain untouched.
    instance._append_log = lambda *_args: None
    return instance


def test_collect_runs_off_ui_thread_and_keeps_queue_untouched(tmp_path, monkeypatch):
    instance = app(tmp_path)
    running = instance.process
    started = []

    class Thread:
        def __init__(self, target, daemon):
            assert daemon is True
            self.target = target

        def start(self):
            started.append(True)
            self.target()

    monkeypatch.setattr(gui.threading, "Thread", Thread)
    monkeypatch.setattr(gui, "save_report", lambda _root: tmp_path / "report.zip")
    instance._collect_diagnostics()
    assert started == [True]
    assert instance.diagnostics_button.values["state"] == "disabled"
    assert instance.events.get_nowait() == ("diagnostics_ready", tmp_path / "report.zip")
    assert instance.process is running


def test_success_and_failure_restore_button_without_stopping_worker(tmp_path, monkeypatch):
    instance = app(tmp_path)
    running = instance.process
    notices = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **kw: notices.append(a))
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **kw: notices.append(a))
    monkeypatch.setattr(gui, "show_report_in_folder", lambda _path: None)
    instance.events.put(("diagnostics_ready", tmp_path / "report.zip"))
    instance._poll_events()
    assert instance.diagnostics_button.values["state"] == "normal"
    instance.events.put(("diagnostics_error", "PermissionError"))
    instance._poll_events()
    assert len(notices) == 2
    assert instance.process is running


def test_background_failure_does_not_leak_exception_secrets(tmp_path, monkeypatch):
    instance = app(tmp_path)

    def fail(_root):
        raise OSError("password secret")

    monkeypatch.setattr(gui, "save_report", fail)

    class Thread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(gui.threading, "Thread", Thread)
    instance._collect_diagnostics()
    assert instance.events.get_nowait() == ("diagnostics_error", "OSError")
