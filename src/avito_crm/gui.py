from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import END, filedialog, messagebox, ttk

from avito_crm.gui_config import (
    browser_profile_dir,
    browser_profile_is_initialized,
    extract_spreadsheet_id,
    google_sheet_url,
    parse_captcha_wait_hours,
    parse_limit,
    parse_max_recipient_ids,
    parse_notification_emails,
    parse_smtp_port,
    parse_telegram_chat_ids,
    parse_telegram_reminders,
    read_env_values,
    service_account_email,
    update_env_values,
)

APP_TITLE = "Avito → CRM"
GOOGLE_CREDENTIALS_URL = "https://console.cloud.google.com/iam-admin/serviceaccounts"
GOOGLE_SHEETS_API_URL = "https://console.cloud.google.com/apis/library/sheets.googleapis.com"
TELEGRAM_BOTFATHER_URL = "https://t.me/BotFather"
MAX_BUSINESS_URL = "https://business.max.ru"
MAX_API_DOCS_URL = "https://dev.max.ru/docs-api"
YANDEX_APP_PASSWORD_URL = "https://id.yandex.ru/security/app-passwords"
YANDEX_SMTP_HELP_URL = (
    "https://yandex.ru/support/yandex-360/business/mail/ru/mail-clients/shared-mailboxes"
)


class DesktopApp:
    def __init__(self, root: tk.Tk, project_root: Path) -> None:
        self.root = root
        self.project_root = project_root.resolve()
        self.env_path = self.project_root / ".env"
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.process_kind = ""
        self.close_requested = False

        values = read_env_values(self.env_path)
        spreadsheet_id = values.get("GOOGLE_SPREADSHEET_ID", "")
        self.sheet_var = tk.StringVar(
            value=google_sheet_url(spreadsheet_id) if spreadsheet_id else ""
        )
        self.worksheet_var = tk.StringVar(value=values.get("GOOGLE_WORKSHEET", "Лист1"))
        self.credentials_var = tk.StringVar(value=values.get("GOOGLE_CREDENTIALS_FILE", ""))
        self.limit_var = tk.StringVar(value=values.get("GUI_DEFAULT_LIMIT", "10"))
        self.retry_manual_var = tk.BooleanVar(
            value=values.get("GUI_RETRY_MANUAL", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.telegram_token_var = tk.StringVar(value=values.get("TELEGRAM_BOT_TOKEN", ""))
        self.telegram_primary_var = tk.StringVar(value=values.get("TELEGRAM_PRIMARY_CHAT_IDS", ""))
        self.telegram_backup_var = tk.StringVar(value=values.get("TELEGRAM_BACKUP_CHAT_IDS", ""))
        self.max_token_var = tk.StringVar(value=values.get("MAX_BOT_TOKEN", ""))
        self.max_primary_var = tk.StringVar(value=values.get("MAX_PRIMARY_RECIPIENTS", ""))
        self.max_backup_var = tk.StringVar(value=values.get("MAX_BACKUP_RECIPIENTS", ""))
        self.telegram_reminders_var = tk.StringVar(
            value=values.get("TELEGRAM_CAPTCHA_REMINDER_MINUTES", "30,60")
        )
        self.captcha_wait_hours_var = tk.StringVar(
            value=self._initial_wait_hours(values.get("AVITO_MANUAL_TIMEOUT_SECONDS", ""))
        )
        self.smtp_host_var = tk.StringVar(value=values.get("SMTP_HOST", "smtp.yandex.ru"))
        self.smtp_port_var = tk.StringVar(value=values.get("SMTP_PORT", "465"))
        self.smtp_security_var = tk.StringVar(
            value=values.get("SMTP_SECURITY", "ssl").strip().upper()
        )
        self.smtp_username_var = tk.StringVar(value=values.get("SMTP_USERNAME", ""))
        self.smtp_password_var = tk.StringVar(value=values.get("SMTP_PASSWORD", ""))
        self.email_primary_var = tk.StringVar(value=values.get("EMAIL_PRIMARY_RECIPIENTS", ""))
        self.email_backup_var = tk.StringVar(value=values.get("EMAIL_BACKUP_RECIPIENTS", ""))
        self.telegram_summary_var = tk.StringVar()
        self.avito_profile_var = tk.StringVar()
        self.telegram_dialog: tk.Toplevel | None = None
        self.max_dialog: tk.Toplevel | None = None
        self.email_dialog: tk.Toplevel | None = None
        self.email_var = tk.StringVar(value="Сервисный email пока не определён")
        self.status_var = tk.StringVar(value="Готово к настройке")

        self._configure_window()
        self._configure_styles()
        self._build_layout()
        self._refresh_avito_profile_status()
        self._refresh_notification_summary()
        self._refresh_service_email(show_error=False)
        self.root.after(100, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_window(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("1040x820")
        self.root.minsize(860, 640)
        self.root.configure(bg="#F4F6FA")

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        available = style.theme_names()
        if "vista" in available:
            style.theme_use("vista")
        elif "clam" in available:
            style.theme_use("clam")

        style.configure("App.TFrame", background="#F4F6FA")
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure(
            "Title.TLabel",
            background="#F4F6FA",
            foreground="#111827",
            font=("Segoe UI Semibold", 24),
        )
        style.configure(
            "Subtitle.TLabel",
            background="#F4F6FA",
            foreground="#667085",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Section.TLabel",
            background="#FFFFFF",
            foreground="#182230",
            font=("Segoe UI Semibold", 12),
        )
        style.configure(
            "Field.TLabel",
            background="#FFFFFF",
            foreground="#344054",
            font=("Segoe UI", 9),
        )
        style.configure(
            "Hint.TLabel",
            background="#FFFFFF",
            foreground="#667085",
            font=("Segoe UI", 8),
        )
        style.configure(
            "Status.TLabel",
            background="#EEF4FF",
            foreground="#175CD3",
            padding=(12, 7),
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "Primary.TButton",
            font=("Segoe UI Semibold", 10),
            padding=(18, 10),
        )
        style.configure("Secondary.TButton", font=("Segoe UI", 9), padding=(12, 8))
        style.configure("Danger.TButton", font=("Segoe UI Semibold", 9), padding=(12, 8))
        style.configure(
            "Body.TCheckbutton",
            background="#F4F6FA",
            foreground="#475467",
            font=("Segoe UI", 9),
        )
        style.configure("TEntry", padding=7)
        style.configure("TSpinbox", padding=7)

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(28, 22, 28, 24))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        header = ttk.Frame(outer, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text=(
                "Google Sheets → Avito → телефон → LPTracker. "
                "Одна строка за раз, с продолжением по статусам."
            ),
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").grid(
            row=0, column=1, rowspan=2, sticky="e"
        )

        settings_card = ttk.Frame(outer, style="Card.TFrame", padding=20)
        settings_card.grid(row=1, column=0, sticky="ew")
        settings_card.columnconfigure(1, weight=1)
        ttk.Label(settings_card, text="Источник и доступ", style="Section.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 14)
        )

        ttk.Label(settings_card, text="Google-таблица", style="Field.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 12)
        )
        self.sheet_entry = ttk.Entry(settings_card, textvariable=self.sheet_var)
        self.sheet_entry.grid(row=1, column=1, columnspan=2, sticky="ew")
        ttk.Button(
            settings_card,
            text="Открыть",
            command=self._open_sheet,
            style="Secondary.TButton",
        ).grid(row=1, column=3, sticky="e", padx=(10, 0))

        ttk.Label(settings_card, text="Лист", style="Field.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 12), pady=(12, 0)
        )
        ttk.Entry(settings_card, textvariable=self.worksheet_var, width=24).grid(
            row=2, column=1, sticky="w", pady=(12, 0)
        )
        ttk.Label(settings_card, text="Лимит новых лидов", style="Field.TLabel").grid(
            row=2, column=2, sticky="e", padx=(20, 12), pady=(12, 0)
        )
        ttk.Spinbox(
            settings_card,
            from_=1,
            to=10_000,
            textvariable=self.limit_var,
            width=10,
        ).grid(row=2, column=3, sticky="e", pady=(12, 0))

        ttk.Label(settings_card, text="Google service account JSON", style="Field.TLabel").grid(
            row=3, column=0, sticky="w", padx=(0, 12), pady=(12, 0)
        )
        ttk.Entry(settings_card, textvariable=self.credentials_var).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=(12, 0)
        )
        ttk.Button(
            settings_card,
            text="Выбрать JSON",
            command=self._select_credentials,
            style="Secondary.TButton",
        ).grid(row=3, column=3, sticky="e", padx=(10, 0), pady=(12, 0))

        email_row = ttk.Frame(settings_card, style="Card.TFrame")
        email_row.grid(row=4, column=1, columnspan=3, sticky="ew", pady=(8, 0))
        email_row.columnconfigure(0, weight=1)
        ttk.Label(email_row, textvariable=self.email_var, style="Hint.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            email_row,
            text="Скопировать email",
            command=self._copy_email,
            style="Secondary.TButton",
        ).grid(row=0, column=1, padx=(10, 0))
        ttk.Button(
            email_row,
            text="Как настроить Google",
            command=self._show_google_help,
            style="Secondary.TButton",
        ).grid(row=0, column=2, padx=(8, 0))

        profile_row = ttk.Frame(settings_card, style="Card.TFrame")
        profile_row.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        profile_row.columnconfigure(1, weight=1)
        ttk.Label(profile_row, text="Профиль Avito", style="Field.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12)
        )
        ttk.Label(
            profile_row,
            textvariable=self.avito_profile_var,
            style="Hint.TLabel",
        ).grid(row=0, column=1, sticky="w")
        self.avito_profile_button = ttk.Button(
            profile_row,
            text="Открыть профиль",
            command=self._open_avito_profile,
            style="Secondary.TButton",
        )
        self.avito_profile_button.grid(row=0, column=2, sticky="e", padx=(10, 0))

        notification_row = ttk.Frame(settings_card, style="Card.TFrame")
        notification_row.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        notification_row.columnconfigure(1, weight=1)
        ttk.Label(
            notification_row,
            text="Капча и уведомления",
            style="Field.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Label(
            notification_row,
            textvariable=self.telegram_summary_var,
            style="Hint.TLabel",
        ).grid(row=0, column=1, columnspan=3, sticky="w")
        self.telegram_button = ttk.Button(
            notification_row,
            text="Настроить Telegram",
            command=self._show_telegram_settings,
            style="Secondary.TButton",
        )
        self.telegram_button.grid(row=1, column=3, sticky="e", padx=(8, 0), pady=(8, 0))
        self.email_button = ttk.Button(
            notification_row,
            text="Настроить Email",
            command=self._show_email_settings,
            style="Secondary.TButton",
        )
        self.email_button.grid(row=1, column=2, sticky="e", padx=(8, 0), pady=(8, 0))
        self.max_button = ttk.Button(
            notification_row,
            text="Настроить MAX",
            command=self._show_max_settings,
            style="Secondary.TButton",
        )
        self.max_button.grid(row=1, column=1, sticky="e", pady=(8, 0))

        controls = ttk.Frame(outer, style="App.TFrame")
        controls.grid(row=2, column=0, sticky="ew", pady=16)
        controls.columnconfigure(4, weight=1)
        self.start_button = tk.Button(
            controls,
            text="▶  Запустить в CRM",
            command=self._start_live,
            bg="#155EEF",
            fg="#FFFFFF",
            activebackground="#004EEB",
            activeforeground="#FFFFFF",
            disabledforeground="#D0D5DD",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("Segoe UI Semibold", 10),
            padx=18,
            pady=10,
            cursor="hand2",
        )
        self.start_button.grid(row=0, column=0, sticky="w")
        self.verify_button = ttk.Button(
            controls,
            text="Проверить доступ",
            command=self._start_verify,
            style="Secondary.TButton",
        )
        self.verify_button.grid(row=0, column=1, padx=(10, 0))
        self.stop_button = ttk.Button(
            controls,
            text="Остановить",
            command=self._request_stop,
            style="Danger.TButton",
            state="disabled",
        )
        self.stop_button.grid(row=0, column=2, padx=(10, 0))
        ttk.Checkbutton(
            controls,
            text="Повторить строки, ожидающие ручной проверки",
            variable=self.retry_manual_var,
            style="Body.TCheckbutton",
        ).grid(row=0, column=3, padx=(18, 0))
        self.progress = ttk.Progressbar(controls, mode="indeterminate", length=130)
        self.progress.grid(row=0, column=4, sticky="e")
        self.progress.grid_remove()

        log_card = ttk.Frame(outer, style="Card.TFrame", padding=(18, 16))
        log_card.grid(row=3, column=0, sticky="nsew")
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)
        ttk.Label(log_card, text="Ход работы", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )
        self.log = tk.Text(
            log_card,
            wrap="word",
            bg="#101828",
            fg="#D0D5DD",
            insertbackground="#FFFFFF",
            selectbackground="#344054",
            relief="flat",
            borderwidth=0,
            font=("Cascadia Mono", 9),
            padx=14,
            pady=12,
            state="disabled",
        )
        scrollbar = ttk.Scrollbar(log_card, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.log.tag_configure("error", foreground="#FDA29B")
        self.log.tag_configure("warning", foreground="#FEC84B")
        self.log.tag_configure("success", foreground="#6CE9A6")
        self._append_log(
            "1. При желании откройте профиль Avito и войдите или выйдите.  "
            "2. Укажите таблицу и JSON.  3. Проверьте доступ и запускайте.\n"
        )

    def _refresh_avito_profile_status(self) -> None:
        values = read_env_values(self.env_path)
        path = browser_profile_dir(self.project_root, values)
        if browser_profile_is_initialized(path):
            self.avito_profile_var.set("сохранён; вход в аккаунт необязателен")
        else:
            self.avito_profile_var.set("ещё не создан; можно работать без входа")

    def _open_avito_profile(self) -> None:
        self._start_process("avito-profile", ["avito-profile"])

    def _select_credentials(self) -> None:
        selected = filedialog.askopenfilename(
            title="Выберите JSON-ключ Google service account",
            filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")],
        )
        if selected:
            self.credentials_var.set(selected)
            self._refresh_service_email(show_error=True)

    def _refresh_service_email(self, *, show_error: bool) -> str:
        raw_path = self.credentials_var.get().strip()
        if not raw_path:
            self.email_var.set("Сервисный email пока не определён")
            return ""
        try:
            email = service_account_email(Path(raw_path).expanduser())
        except ValueError as exc:
            self.email_var.set(str(exc))
            if show_error:
                messagebox.showerror("Google JSON", str(exc), parent=self.root)
            return ""
        self.email_var.set(f"Откройте таблицу для редактирования: {email}")
        return email

    def _copy_email(self) -> None:
        email = self._refresh_service_email(show_error=True)
        if not email:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(email)
        self.status_var.set("Email скопирован")

    def _show_google_help(self) -> None:
        answer = messagebox.askyesno(
            "Настройка Google Sheets",
            (
                "1. В Google Cloud включите Google Sheets API.\n"
                "2. Создайте service account и JSON-ключ.\n"
                "3. Выберите JSON в этом окне.\n"
                "4. Скопируйте показанный email и дайте ему в таблице "
                "роль «Редактор».\n\n"
                "Открыть Google Cloud?"
            ),
            parent=self.root,
        )
        if answer:
            webbrowser.open(GOOGLE_SHEETS_API_URL)
            webbrowser.open(GOOGLE_CREDENTIALS_URL)

    @staticmethod
    def _initial_wait_hours(raw_seconds: str) -> str:
        try:
            hours = float(raw_seconds) / 3600 if raw_seconds.strip() else 12.0
        except ValueError:
            hours = 12.0
        # Older installations used five minutes. Migrate them to an unattended-safe value.
        if hours < 1:
            hours = 12.0
        return f"{hours:g}"

    def _refresh_notification_summary(self) -> None:
        channels: list[str] = []
        if self.max_token_var.get().strip() and self.max_primary_var.get().strip():
            channels.append("MAX")
        if (
            self.smtp_username_var.get().strip()
            and self.smtp_password_var.get()
            and self.email_primary_var.get().strip()
        ):
            channels.append("Email")
        if self.telegram_token_var.get().strip() and self.telegram_primary_var.get().strip():
            channels.append("Telegram")
        if not channels:
            self.telegram_summary_var.set(
                "не настроено — ожидание работает, сообщения не отправляются"
            )
            return
        reminders = self.telegram_reminders_var.get().strip() or "30,60"
        wait_hours = self.captcha_wait_hours_var.get().strip() or "12"
        has_backup = (
            self.max_backup_var.get().strip()
            or self.telegram_backup_var.get().strip()
            or self.email_backup_var.get().strip()
        )
        backup = "; есть резервный получатель" if has_backup else ""
        self.telegram_summary_var.set(
            f"{' + '.join(channels)}: сразу + {reminders} мин; до {wait_hours} ч{backup}"
        )

    def _show_telegram_settings(self) -> None:
        if self.telegram_dialog and self.telegram_dialog.winfo_exists():
            self.telegram_dialog.lift()
            self.telegram_dialog.focus_force()
            return

        dialog = tk.Toplevel(self.root)
        self.telegram_dialog = dialog
        dialog.title("Telegram и ожидание капчи")
        dialog.geometry("780x430")
        dialog.minsize(700, 420)
        dialog.transient(self.root)
        dialog.configure(bg="#F4F6FA")

        card = ttk.Frame(dialog, style="Card.TFrame", padding=22)
        card.pack(fill="both", expand=True, padx=20, pady=20)
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text="Уведомления о капче", style="Section.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            card,
            text=(
                "Сразу пишем основному ответственному, затем напоминаем. "
                "Последнее напоминание уходит также резервному человеку."
            ),
            style="Hint.TLabel",
            wraplength=700,
            justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 18))

        ttk.Label(card, text="Токен бота", style="Field.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 14)
        )
        ttk.Entry(card, textvariable=self.telegram_token_var, show="●").grid(
            row=2, column=1, sticky="ew"
        )
        ttk.Button(
            card,
            text="Открыть BotFather",
            command=lambda: webbrowser.open(TELEGRAM_BOTFATHER_URL),
            style="Secondary.TButton",
        ).grid(row=2, column=2, padx=(10, 0))

        ttk.Label(card, text="Основные Chat ID", style="Field.TLabel").grid(
            row=3, column=0, sticky="w", padx=(0, 14), pady=(12, 0)
        )
        ttk.Entry(card, textvariable=self.telegram_primary_var).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=(12, 0)
        )
        ttk.Label(card, text="Резервные Chat ID", style="Field.TLabel").grid(
            row=4, column=0, sticky="w", padx=(0, 14), pady=(12, 0)
        )
        ttk.Entry(card, textvariable=self.telegram_backup_var).grid(
            row=4, column=1, columnspan=2, sticky="ew", pady=(12, 0)
        )
        ttk.Label(
            card,
            text=(
                "Несколько ID можно указать через запятую. "
                "Каждый человек сначала пишет боту /start."
            ),
            style="Hint.TLabel",
        ).grid(row=5, column=1, columnspan=2, sticky="w", pady=(5, 0))

        timings = ttk.Frame(card, style="Card.TFrame")
        timings.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        timings.columnconfigure(1, weight=1)
        ttk.Label(timings, text="Напоминания, мин", style="Field.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12)
        )
        ttk.Entry(timings, textvariable=self.telegram_reminders_var, width=18).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(timings, text="Максимально ждать, часов", style="Field.TLabel").grid(
            row=0, column=2, sticky="e", padx=(24, 12)
        )
        ttk.Entry(timings, textvariable=self.captcha_wait_hours_var, width=10).grid(
            row=0, column=3, sticky="e"
        )

        ttk.Label(
            card,
            text=(
                "После решения капчи программа сама заметит, что проверка исчезла, "
                "отправит сообщение «продолжаем» и откроет следующую ссылку."
            ),
            style="Hint.TLabel",
            wraplength=700,
            justify="left",
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(16, 0))

        buttons = ttk.Frame(card, style="Card.TFrame")
        buttons.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(22, 0))
        buttons.columnconfigure(0, weight=1)
        ttk.Button(
            buttons,
            text="Найти Chat ID",
            command=self._start_telegram_chats,
            style="Secondary.TButton",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            buttons,
            text="Отправить тест",
            command=self._start_telegram_test,
            style="Secondary.TButton",
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(
            buttons,
            text="Отмена",
            command=dialog.destroy,
            style="Secondary.TButton",
        ).grid(row=0, column=2, padx=(16, 0))
        ttk.Button(
            buttons,
            text="Сохранить",
            command=self._save_telegram_dialog,
            style="Primary.TButton",
        ).grid(row=0, column=3, padx=(8, 0))

        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()

    def _show_max_settings(self) -> None:
        if self.max_dialog and self.max_dialog.winfo_exists():
            self.max_dialog.lift()
            self.max_dialog.focus_force()
            return

        dialog = tk.Toplevel(self.root)
        self.max_dialog = dialog
        dialog.title("MAX и ожидание капчи")
        dialog.geometry("800x470")
        dialog.minsize(720, 450)
        dialog.transient(self.root)
        dialog.configure(bg="#F4F6FA")

        card = ttk.Frame(dialog, style="Card.TFrame", padding=22)
        card.pack(fill="both", expand=True, padx=20, pady=20)
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text="Уведомления в MAX", style="Section.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            card,
            text=(
                "MAX отправляется первым, затем срабатывают Email и Telegram. "
                "Токен хранится только на этом компьютере."
            ),
            style="Hint.TLabel",
            wraplength=720,
            justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 18))

        ttk.Label(card, text="Токен MAX-бота", style="Field.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 14)
        )
        ttk.Entry(card, textvariable=self.max_token_var, show="●").grid(
            row=2, column=1, sticky="ew"
        )
        ttk.Button(
            card,
            text="MAX для бизнеса",
            command=lambda: webbrowser.open(MAX_BUSINESS_URL),
            style="Secondary.TButton",
        ).grid(row=2, column=2, padx=(10, 0))

        ttk.Label(card, text="Основные ID", style="Field.TLabel").grid(
            row=3, column=0, sticky="w", padx=(0, 14), pady=(12, 0)
        )
        ttk.Entry(card, textvariable=self.max_primary_var).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=(12, 0)
        )
        ttk.Label(card, text="Резервные ID", style="Field.TLabel").grid(
            row=4, column=0, sticky="w", padx=(0, 14), pady=(12, 0)
        )
        ttk.Entry(card, textvariable=self.max_backup_var).grid(
            row=4, column=1, columnspan=2, sticky="ew", pady=(12, 0)
        )
        ttk.Label(
            card,
            text=(
                "Личный получатель: user:123; группа: chat:456. Несколько ID — через запятую. "
                "Сотрудник сначала открывает бота и нажимает «Начать»."
            ),
            style="Hint.TLabel",
            wraplength=650,
            justify="left",
        ).grid(row=5, column=1, columnspan=2, sticky="w", pady=(5, 0))

        timings = ttk.Frame(card, style="Card.TFrame")
        timings.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        timings.columnconfigure(1, weight=1)
        ttk.Label(timings, text="Напоминания, мин", style="Field.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12)
        )
        ttk.Entry(timings, textvariable=self.telegram_reminders_var, width=18).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(timings, text="Максимально ждать, часов", style="Field.TLabel").grid(
            row=0, column=2, sticky="e", padx=(24, 12)
        )
        ttk.Entry(timings, textvariable=self.captcha_wait_hours_var, width=10).grid(
            row=0, column=3, sticky="e"
        )

        buttons = ttk.Frame(card, style="Card.TFrame")
        buttons.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(22, 0))
        buttons.columnconfigure(0, weight=1)
        ttk.Button(
            buttons,
            text="Документация MAX",
            command=lambda: webbrowser.open(MAX_API_DOCS_URL),
            style="Secondary.TButton",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            buttons,
            text="Найти ID",
            command=self._start_max_recipients,
            style="Secondary.TButton",
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(
            buttons,
            text="Отправить тест",
            command=self._start_max_test,
            style="Secondary.TButton",
        ).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(
            buttons,
            text="Отмена",
            command=dialog.destroy,
            style="Secondary.TButton",
        ).grid(row=0, column=3, padx=(16, 0))
        ttk.Button(
            buttons,
            text="Сохранить",
            command=self._save_max_dialog,
            style="Primary.TButton",
        ).grid(row=0, column=4, padx=(8, 0))

        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()

    def _show_email_settings(self) -> None:
        if self.email_dialog and self.email_dialog.winfo_exists():
            self.email_dialog.lift()
            self.email_dialog.focus_force()
            return

        dialog = tk.Toplevel(self.root)
        self.email_dialog = dialog
        dialog.title("Email и ожидание капчи")
        dialog.geometry("820x560")
        dialog.minsize(760, 540)
        dialog.transient(self.root)
        dialog.configure(bg="#F4F6FA")

        card = ttk.Frame(dialog, style="Card.TFrame", padding=22)
        card.pack(fill="both", expand=True, padx=20, pady=20)
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text="Email-уведомления о капче", style="Section.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            card,
            text=(
                "Почта доступна на этом сервере и служит надёжным резервом после MAX. "
                "Для Яндекса нужен отдельный пароль приложения, а не пароль от аккаунта."
            ),
            style="Hint.TLabel",
            wraplength=740,
            justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 18))

        ttk.Label(card, text="SMTP-сервер", style="Field.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 14)
        )
        ttk.Entry(card, textvariable=self.smtp_host_var).grid(row=2, column=1, sticky="ew")
        ttk.Label(card, text="Порт", style="Field.TLabel").grid(
            row=2, column=2, sticky="e", padx=(18, 10)
        )
        ttk.Entry(card, textvariable=self.smtp_port_var, width=8).grid(row=2, column=3, sticky="e")

        ttk.Label(card, text="Защита", style="Field.TLabel").grid(
            row=3, column=0, sticky="w", padx=(0, 14), pady=(12, 0)
        )
        ttk.Combobox(
            card,
            textvariable=self.smtp_security_var,
            values=("SSL", "STARTTLS"),
            state="readonly",
            width=14,
        ).grid(row=3, column=1, sticky="w", pady=(12, 0))

        ttk.Label(card, text="Логин / email", style="Field.TLabel").grid(
            row=4, column=0, sticky="w", padx=(0, 14), pady=(12, 0)
        )
        ttk.Entry(card, textvariable=self.smtp_username_var).grid(
            row=4, column=1, columnspan=3, sticky="ew", pady=(12, 0)
        )
        ttk.Label(card, text="Пароль приложения", style="Field.TLabel").grid(
            row=5, column=0, sticky="w", padx=(0, 14), pady=(12, 0)
        )
        ttk.Entry(card, textvariable=self.smtp_password_var, show="●").grid(
            row=5, column=1, columnspan=2, sticky="ew", pady=(12, 0)
        )
        ttk.Button(
            card,
            text="Создать пароль",
            command=lambda: webbrowser.open(YANDEX_APP_PASSWORD_URL),
            style="Secondary.TButton",
        ).grid(row=5, column=3, sticky="e", padx=(10, 0), pady=(12, 0))

        ttk.Label(card, text="Основные получатели", style="Field.TLabel").grid(
            row=6, column=0, sticky="w", padx=(0, 14), pady=(12, 0)
        )
        ttk.Entry(card, textvariable=self.email_primary_var).grid(
            row=6, column=1, columnspan=3, sticky="ew", pady=(12, 0)
        )
        ttk.Label(card, text="Резервные получатели", style="Field.TLabel").grid(
            row=7, column=0, sticky="w", padx=(0, 14), pady=(12, 0)
        )
        ttk.Entry(card, textvariable=self.email_backup_var).grid(
            row=7, column=1, columnspan=3, sticky="ew", pady=(12, 0)
        )
        ttk.Label(
            card,
            text="Несколько адресов можно указать через запятую.",
            style="Hint.TLabel",
        ).grid(row=8, column=1, columnspan=3, sticky="w", pady=(5, 0))

        timings = ttk.Frame(card, style="Card.TFrame")
        timings.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(16, 0))
        timings.columnconfigure(1, weight=1)
        ttk.Label(timings, text="Напоминания, мин", style="Field.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12)
        )
        ttk.Entry(timings, textvariable=self.telegram_reminders_var, width=18).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(timings, text="Максимально ждать, часов", style="Field.TLabel").grid(
            row=0, column=2, sticky="e", padx=(24, 12)
        )
        ttk.Entry(timings, textvariable=self.captcha_wait_hours_var, width=10).grid(
            row=0, column=3, sticky="e"
        )

        buttons = ttk.Frame(card, style="Card.TFrame")
        buttons.grid(row=10, column=0, columnspan=4, sticky="ew", pady=(22, 0))
        buttons.columnconfigure(1, weight=1)
        ttk.Button(
            buttons,
            text="Инструкция Яндекса",
            command=lambda: webbrowser.open(YANDEX_SMTP_HELP_URL),
            style="Secondary.TButton",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            buttons,
            text="Отправить тест",
            command=self._start_email_test,
            style="Secondary.TButton",
        ).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(
            buttons,
            text="Отмена",
            command=dialog.destroy,
            style="Secondary.TButton",
        ).grid(row=0, column=3, padx=(16, 0))
        ttk.Button(
            buttons,
            text="Сохранить",
            command=self._save_email_dialog,
            style="Primary.TButton",
        ).grid(row=0, column=4, padx=(8, 0))

        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()

    def _timing_env_values(self) -> dict[str, str]:
        reminders = parse_telegram_reminders(self.telegram_reminders_var.get())
        wait_hours = parse_captcha_wait_hours(self.captcha_wait_hours_var.get())
        wait_seconds = wait_hours * 3600
        if reminders[-1] * 60 >= wait_seconds:
            raise ValueError("Последнее напоминание должно быть раньше окончания ожидания")
        return {
            "TELEGRAM_CAPTCHA_REMINDER_MINUTES": ",".join(f"{item:g}" for item in reminders),
            "AVITO_MANUAL_TIMEOUT_SECONDS": f"{wait_seconds:g}",
        }

    def _telegram_env_values(
        self, *, require_token: bool = False, require_primary: bool = False
    ) -> dict[str, str]:
        token = self.telegram_token_var.get().strip()
        if "\r" in token or "\n" in token:
            raise ValueError("Токен Telegram должен занимать одну строку")
        primary = parse_telegram_chat_ids(self.telegram_primary_var.get())
        backup = parse_telegram_chat_ids(self.telegram_backup_var.get())
        if require_token and not token:
            raise ValueError("Сначала вставьте токен бота от BotFather")
        if (primary or backup) and not token:
            raise ValueError("Для Telegram Chat ID необходимо указать токен бота")
        if require_primary and not primary:
            raise ValueError("Укажите хотя бы один основной Chat ID")
        return {
            "TELEGRAM_BOT_TOKEN": token,
            "TELEGRAM_PRIMARY_CHAT_IDS": ",".join(primary),
            "TELEGRAM_BACKUP_CHAT_IDS": ",".join(backup),
        }

    def _max_env_values(
        self, *, require_token: bool = False, require_primary: bool = False
    ) -> dict[str, str]:
        token = self.max_token_var.get().strip()
        if "\r" in token or "\n" in token:
            raise ValueError("Токен MAX должен занимать одну строку")
        primary = parse_max_recipient_ids(self.max_primary_var.get())
        backup = parse_max_recipient_ids(self.max_backup_var.get())
        if require_token and not token:
            raise ValueError("Сначала вставьте токен MAX-бота")
        if (primary or backup) and not token:
            raise ValueError("Для MAX ID необходимо указать токен бота")
        if backup and not primary:
            raise ValueError("Сначала укажите хотя бы один основной MAX ID")
        if require_primary and not primary:
            raise ValueError("Укажите хотя бы один основной MAX ID")
        return {
            "MAX_API_BASE_URL": "https://platform-api2.max.ru",
            "MAX_BOT_TOKEN": token,
            "MAX_PRIMARY_RECIPIENTS": ",".join(primary),
            "MAX_BACKUP_RECIPIENTS": ",".join(backup),
        }

    def _email_env_values(self, *, require_credentials: bool = False) -> dict[str, str]:
        host = self.smtp_host_var.get().strip()
        port = parse_smtp_port(self.smtp_port_var.get())
        security = self.smtp_security_var.get().strip().lower()
        if security not in {"ssl", "starttls"}:
            raise ValueError("Выберите SSL или STARTTLS")
        username = self.smtp_username_var.get().strip()
        password = self.smtp_password_var.get()
        if "\r" in password or "\n" in password:
            raise ValueError("Пароль приложения должен занимать одну строку")
        primary = parse_notification_emails(self.email_primary_var.get())
        backup = parse_notification_emails(self.email_backup_var.get())
        configured = bool(username or password or primary or backup)
        if require_credentials or configured:
            if not host:
                raise ValueError("Укажите SMTP-сервер")
            if not username:
                raise ValueError("Укажите полный email в поле «Логин / email»")
            usernames = parse_notification_emails(username)
            if len(usernames) != 1:
                raise ValueError("SMTP-логин должен быть одним полным email")
            if not password:
                raise ValueError("Укажите пароль приложения для почты")
            if not primary:
                raise ValueError("Укажите хотя бы одного основного получателя email")
        return {
            "SMTP_HOST": host,
            "SMTP_PORT": str(port),
            "SMTP_SECURITY": security,
            "SMTP_USERNAME": username,
            "SMTP_PASSWORD": password,
            "SMTP_FROM_ADDRESS": username,
            "EMAIL_PRIMARY_RECIPIENTS": ",".join(primary),
            "EMAIL_BACKUP_RECIPIENTS": ",".join(backup),
        }

    def _notification_env_values(
        self, *, require_token: bool = False, require_primary: bool = False
    ) -> dict[str, str]:
        return {
            **self._telegram_env_values(
                require_token=require_token,
                require_primary=require_primary,
            ),
            **self._max_env_values(),
            **self._email_env_values(),
            **self._timing_env_values(),
        }

    def _save_telegram_settings(
        self, *, require_token: bool = False, require_primary: bool = False
    ) -> None:
        updates = {
            **self._telegram_env_values(
                require_token=require_token,
                require_primary=require_primary,
            ),
            **self._timing_env_values(),
        }
        update_env_values(self.env_path, updates)
        self._refresh_notification_summary()

    def _save_email_settings(self, *, require_credentials: bool = False) -> None:
        updates = {
            **self._email_env_values(require_credentials=require_credentials),
            **self._timing_env_values(),
        }
        update_env_values(self.env_path, updates)
        self._refresh_notification_summary()

    def _save_max_settings(
        self, *, require_token: bool = False, require_primary: bool = False
    ) -> None:
        updates = {
            **self._max_env_values(
                require_token=require_token,
                require_primary=require_primary,
            ),
            **self._timing_env_values(),
        }
        update_env_values(self.env_path, updates)
        self._refresh_notification_summary()

    def _save_telegram_dialog(self) -> None:
        try:
            self._save_telegram_settings()
        except (OSError, ValueError) as exc:
            messagebox.showerror("Telegram", str(exc), parent=self.telegram_dialog or self.root)
            return
        if self.telegram_dialog:
            self.telegram_dialog.destroy()
        self.status_var.set("Настройки Telegram сохранены")

    def _start_telegram_chats(self) -> None:
        try:
            self._save_telegram_settings(require_token=True)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Telegram", str(exc), parent=self.telegram_dialog or self.root)
            return
        if self.telegram_dialog:
            self.telegram_dialog.destroy()
        self._start_process("telegram-chats", ["telegram-chats"])

    def _start_telegram_test(self) -> None:
        try:
            self._save_telegram_settings(require_token=True, require_primary=True)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Telegram", str(exc), parent=self.telegram_dialog or self.root)
            return
        if self.telegram_dialog:
            self.telegram_dialog.destroy()
        self._start_process("telegram-test", ["telegram-test"])

    def _save_email_dialog(self) -> None:
        try:
            self._save_email_settings()
        except (OSError, ValueError) as exc:
            messagebox.showerror("Email", str(exc), parent=self.email_dialog or self.root)
            return
        if self.email_dialog:
            self.email_dialog.destroy()
        self.status_var.set("Настройки Email сохранены")

    def _start_email_test(self) -> None:
        try:
            self._save_email_settings(require_credentials=True)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Email", str(exc), parent=self.email_dialog or self.root)
            return
        if self.email_dialog:
            self.email_dialog.destroy()
        self._start_process("email-test", ["email-test"])

    def _save_max_dialog(self) -> None:
        try:
            self._save_max_settings()
        except (OSError, ValueError) as exc:
            messagebox.showerror("MAX", str(exc), parent=self.max_dialog or self.root)
            return
        if self.max_dialog:
            self.max_dialog.destroy()
        self.status_var.set("Настройки MAX сохранены")

    def _start_max_recipients(self) -> None:
        try:
            self._save_max_settings(require_token=True)
        except (OSError, ValueError) as exc:
            messagebox.showerror("MAX", str(exc), parent=self.max_dialog or self.root)
            return
        if self.max_dialog:
            self.max_dialog.destroy()
        self._start_process("max-recipients", ["max-recipients"])

    def _start_max_test(self) -> None:
        try:
            self._save_max_settings(require_token=True, require_primary=True)
        except (OSError, ValueError) as exc:
            messagebox.showerror("MAX", str(exc), parent=self.max_dialog or self.root)
            return
        if self.max_dialog:
            self.max_dialog.destroy()
        self._start_process("max-test", ["max-test"])

    def _open_sheet(self) -> None:
        try:
            spreadsheet_id = extract_spreadsheet_id(self.sheet_var.get())
        except ValueError as exc:
            messagebox.showerror("Google Sheets", str(exc), parent=self.root)
            return
        webbrowser.open(google_sheet_url(spreadsheet_id))

    def _validate_and_save(self) -> tuple[str, str, Path, int]:
        spreadsheet_id = extract_spreadsheet_id(self.sheet_var.get())
        worksheet = self.worksheet_var.get().strip()
        if not worksheet:
            raise ValueError("Укажите точное название листа")
        credentials = Path(self.credentials_var.get().strip()).expanduser()
        service_account_email(credentials)
        limit = parse_limit(self.limit_var.get())
        notification_updates = self._notification_env_values()
        update_env_values(
            self.env_path,
            {
                "GOOGLE_CREDENTIALS_FILE": str(credentials.resolve()),
                "GOOGLE_SPREADSHEET_ID": spreadsheet_id,
                "GOOGLE_WORKSHEET": worksheet,
                "GUI_DEFAULT_LIMIT": str(limit),
                "GUI_RETRY_MANUAL": str(self.retry_manual_var.get()).lower(),
                **notification_updates,
            },
        )
        self.sheet_var.set(google_sheet_url(spreadsheet_id))
        return spreadsheet_id, worksheet, credentials, limit

    def _start_verify(self) -> None:
        try:
            _spreadsheet_id, worksheet, _credentials, _limit = self._validate_and_save()
        except (OSError, ValueError) as exc:
            messagebox.showerror("Проверьте настройки", str(exc), parent=self.root)
            return
        self._start_process(
            "verify",
            ["doctor", "--source", "google", "--sheet", worksheet, "--online-crm"],
        )

    def _start_live(self) -> None:
        try:
            _spreadsheet_id, worksheet, _credentials, limit = self._validate_and_save()
        except (OSError, ValueError) as exc:
            messagebox.showerror("Проверьте настройки", str(exc), parent=self.root)
            return
        confirmed = messagebox.askyesno(
            "Запуск в CRM",
            (
                f"Запустить последовательную обработку до {limit} новых лидов?\n\n"
                "Номер будет записан в LPTracker сразу после распознавания."
            ),
            parent=self.root,
        )
        if not confirmed:
            return
        arguments = [
            "run",
            "--source",
            "google",
            "--sheet",
            worksheet,
            "--limit",
            str(limit),
            "--live",
        ]
        if self.retry_manual_var.get():
            arguments.append("--retry-manual")
        self._start_process("live", arguments)

    def _start_process(self, kind: str, arguments: list[str]) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Запуск уже идёт", "Дождитесь его завершения")
            return
        python = self.project_root / ".venv" / "Scripts" / "python.exe"
        if not python.is_file():
            messagebox.showerror(
                "Нет окружения",
                "Не найден .venv\\Scripts\\python.exe. Повторите установку.",
                parent=self.root,
            )
            return

        labels = {
            "verify": ("Проверяем доступ…", "Проверка доступа\n"),
            "live": ("Запускаем…", "Запуск очереди в CRM\n"),
            "telegram-chats": ("Ищем Telegram-чаты…", "Поиск Telegram Chat ID\n"),
            "telegram-test": ("Проверяем Telegram…", "Тест Telegram-уведомлений\n"),
            "email-test": ("Проверяем Email…", "Тест email-уведомлений\n"),
            "max-recipients": ("Ищем MAX ID…", "Поиск получателей MAX\n"),
            "max-test": ("Проверяем MAX…", "Тест MAX-уведомлений\n"),
            "avito-profile": (
                "Профиль Avito открыт…",
                "Управление профилем Avito\n",
            ),
        }
        status_text, log_text = labels.get(kind, ("Выполняем…", "Запуск операции\n"))
        self.process_kind = kind
        self.status_var.set(status_text)
        self._set_running(True)
        self._append_log("\n" + "─" * 72 + "\n")
        self._append_log(log_text, "success")

        command = [str(python), "-m", "avito_crm", *arguments]
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUNBUFFERED"] = "1"
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                command,
                cwd=self.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                creationflags=creation_flags,
            )
        except OSError as exc:
            self.process = None
            self._set_running(False)
            messagebox.showerror("Не удалось запустить", str(exc), parent=self.root)
            return
        threading.Thread(target=self._read_process, daemon=True).start()

    def _read_process(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdout is not None:
            for line in process.stdout:
                self.events.put(("line", line))
        return_code = process.wait()
        self.events.put(("done", (self.process_kind, return_code)))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "line":
                    line = str(payload)
                    self._append_log(line, self._line_tag(line))
                    self._update_status_from_line(line)
                elif kind == "done":
                    process_kind, return_code = payload  # type: ignore[misc]
                    self._process_done(str(process_kind), int(return_code))
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_events)

    def _process_done(self, kind: str, return_code: int) -> None:
        self.process = None
        self._set_running(False)
        if return_code == 0:
            success_status = {
                "verify": "Доступ проверен",
                "live": "Очередь завершена",
                "telegram-chats": "Поиск Chat ID завершён",
                "telegram-test": "Telegram работает",
                "email-test": "Email работает",
                "max-recipients": "Поиск MAX ID завершён",
                "max-test": "MAX работает",
                "avito-profile": "Профиль Avito сохранён",
            }
            self.status_var.set(success_status.get(kind, "Готово"))
            self._append_log("Готово.\n", "success")
        else:
            self.status_var.set("Завершено с ошибками")
            self._append_log(
                f"Процесс завершён с кодом {return_code}. Смотрите ошибку выше.\n",
                "error",
            )
        if kind == "avito-profile":
            self._refresh_avito_profile_status()
        if self.close_requested:
            self.root.destroy()

    def _request_stop(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        if self.process_kind == "avito-profile":
            messagebox.showinfo(
                "Профиль Avito",
                "Закройте все окна синего Chromium. Профиль сохранится автоматически.",
                parent=self.root,
            )
            return
        values = read_env_values(self.env_path)
        raw_data_dir = values.get("APP_DATA_DIR", "").strip()
        data_dir = Path(raw_data_dir).expanduser() if raw_data_dir else self.project_root / "data"
        if not data_dir.is_absolute():
            data_dir = self.project_root / data_dir
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "STOP").write_text("gui-stop", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Не удалось остановить", str(exc), parent=self.root)
            return
        self.status_var.set("Останавливаем после текущей строки…")
        self.stop_button.configure(state="disabled")
        self._append_log(
            "Запрошена мягкая остановка. Текущая строка будет безопасно завершена.\n",
            "warning",
        )

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.start_button.configure(state=state)
        self.verify_button.configure(state=state)
        self.telegram_button.configure(state=state)
        self.email_button.configure(state=state)
        self.max_button.configure(state=state)
        self.avito_profile_button.configure(state=state)
        can_stop = running and self.process_kind != "avito-profile"
        self.stop_button.configure(state="normal" if can_stop else "disabled")
        if running:
            self.progress.grid()
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.grid_remove()

    def _append_log(self, text: str, tag: str = "") -> None:
        self.log.configure(state="normal")
        self.log.insert(END, text, tag or None)
        self.log.see(END)
        self.log.configure(state="disabled")

    @staticmethod
    def _line_tag(line: str) -> str:
        upper = line.upper()
        if "ERROR" in upper or "ОШИБК" in upper:
            return "error"
        if "WARNING" in upper or "ручную проверку" in line.lower():
            return "warning"
        if "лид" in line.lower() and "создан" in line.lower():
            return "success"
        return ""

    def _update_status_from_line(self, line: str) -> None:
        lowered = line.lower()
        if "ручную проверку" in lowered:
            self.status_var.set("Завершите проверку в браузере")
        elif "браузер avito открыт" in lowered:
            self.status_var.set("Войдите, выйдите или оставьте гостевой режим")
        elif "профиль браузера сохранён" in lowered:
            self.status_var.set("Профиль Avito сохранён")
        elif "telegram-сообщение доставлено" in lowered:
            self.status_var.set("Telegram работает")
        elif "email-сообщение доставлено" in lowered:
            self.status_var.set("Email работает")
        elif "max-сообщение доставлено" in lowered:
            self.status_var.set("MAX работает")
        elif "найденные telegram-чаты" in lowered:
            self.status_var.set("Скопируйте Chat ID из журнала")
        elif "найденные получатели max" in lowered:
            self.status_var.set("Скопируйте MAX ID из журнала")
        elif "ручное действие завершено" in lowered:
            self.status_var.set("Проверка пройдена, продолжаем…")
        elif "открываем объявление" in lowered:
            self.status_var.set("Открываем следующую ссылку…")
        elif "номер получен" in lowered:
            self.status_var.set("Номер получен, записываем в CRM…")
        elif "лид" in lowered and "создан" in lowered:
            self.status_var.set("Лид создан, переходим дальше…")
        elif "дубликат" in lowered:
            self.status_var.set("Дубликат пропущен, переходим дальше…")

    def _on_close(self) -> None:
        if self.process and self.process.poll() is None:
            if self.process_kind == "avito-profile":
                messagebox.showinfo(
                    "Профиль Avito открыт",
                    "Сначала закройте все окна синего Chromium, чтобы безопасно сохранить профиль.",
                    parent=self.root,
                )
                return
            confirmed = messagebox.askyesno(
                "Идёт обработка",
                "Запросить мягкую остановку и закрыть окно после текущей строки?",
                parent=self.root,
            )
            if confirmed:
                self.close_requested = True
                self._request_stop()
            return
        self.root.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    root = tk.Tk()
    try:
        DesktopApp(root, args.root)
    except Exception as exc:
        messagebox.showerror("Ошибка запуска", str(exc), parent=root)
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main(sys.argv[1:])
