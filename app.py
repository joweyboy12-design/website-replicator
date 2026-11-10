"""Website Replicator - desktop GUI (Tkinter, no extra dependencies)."""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from replicator.store import Archive, default_root, list_archives
from replicator.util import human_size

APP_TITLE = "Website Replicator"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1100x760")
        self.minsize(900, 620)
        self.log_q: queue.Queue[str] = queue.Queue()
        self.capture_stop: threading.Event | None = None
        self.capture_thread: threading.Thread | None = None
        self.servers: dict[str, object] = {}
        self._build()
        self.refresh_archives()
        self.after(100, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ UI
    def _build(self):
        style = ttk.Style(self)
        with_theme = "vista" if "vista" in style.theme_names() else "clam"
        style.theme_use(with_theme)
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

        pad = dict(padx=6, pady=4)
        cap = ttk.LabelFrame(self, text="Capture a website")
        cap.pack(fill="x", padx=10, pady=(10, 4))

        self.v_url = tk.StringVar()
        self.v_name = tk.StringVar()
        self.v_depth = tk.IntVar(value=1)  # 1 = also save the pages the start page links to
        self.v_pages = tk.IntVar(value=25)
        self.v_wait = tk.DoubleVar(value=8.0)
        self.v_timeout = tk.DoubleVar(value=60.0)
        self.v_maxmb = tk.DoubleVar(value=512.0)
        self.v_show = tk.BooleanVar(value=False)   # headless is faster
        self.v_workers = tk.IntVar(value=8)
        self.v_scroll = tk.BooleanVar(value=True)
        self.v_click = tk.BooleanVar(value=True)

        ttk.Label(cap, text="Target URL").grid(row=0, column=0, sticky="w", **pad)
        url_entry = ttk.Entry(cap, textvariable=self.v_url)
        url_entry.grid(row=0, column=1, columnspan=7, sticky="ew", **pad)
        url_entry.bind("<Return>", lambda _e: self.start_capture())
        ttk.Label(cap, text="Archive name (optional)").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(cap, textvariable=self.v_name).grid(row=1, column=1, columnspan=3, sticky="ew", **pad)

        spin = lambda var, frm, to, inc, r, c, label, w=7: (
            ttk.Label(cap, text=label).grid(row=r, column=c, sticky="e", **pad),
            ttk.Spinbox(cap, textvariable=var, from_=frm, to=to, increment=inc, width=w)
            .grid(row=r, column=c + 1, sticky="w", **pad))
        spin(self.v_depth, 0, 5, 1, 2, 0, "Crawl depth")
        spin(self.v_pages, 1, 500, 1, 2, 2, "Max pages")
        spin(self.v_wait, 0, 300, 1, 2, 4, "WebGL wait (s)")
        spin(self.v_timeout, 10, 600, 5, 2, 6, "Timeout (s)")
        spin(self.v_maxmb, 1, 4096, 16, 1, 4, "Max asset MB")
        spin(self.v_workers, 1, 16, 1, 1, 6, "Parallel tabs")
        ttk.Checkbutton(cap, text="Show browser while capturing", variable=self.v_show).grid(
            row=3, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(cap, text="Auto-scroll + interact (lazy loading)", variable=self.v_scroll).grid(
            row=3, column=2, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(cap, text="Click every button / menu / tab", variable=self.v_click).grid(
            row=4, column=0, columnspan=3, sticky="w", **pad)
        self.btn_capture = ttk.Button(cap, text="Capture", style="Accent.TButton", command=self.start_capture)
        self.btn_capture.grid(row=3, column=6, sticky="ew", **pad)
        self.btn_stop = ttk.Button(cap, text="Stop", command=self.stop_capture, state="disabled")
        self.btn_stop.grid(row=3, column=7, sticky="ew", **pad)
        cap.columnconfigure(1, weight=1)
        cap.columnconfigure(3, weight=1)

        arc = ttk.LabelFrame(self, text=f"Archives  ({default_root()})")
        arc.pack(fill="both", expand=True, padx=10, pady=4)
        cols = ("url", "files", "size", "date", "status")
        self.tree = ttk.Treeview(arc, columns=cols, show="tree headings", height=8, selectmode="browse")
        self.tree.heading("#0", text="Name")
        self.tree.column("#0", width=230)
        for c, w, t in (("url", 330, "Start URL"), ("files", 60, "Files"), ("size", 80, "Size"),
                        ("date", 130, "Captured"), ("status", 140, "Server")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        self.tree.bind("<Double-1>", lambda _e: self.play_offline())
        sb = ttk.Scrollbar(arc, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y", pady=6)
        self.tree.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(arc)
        btns.pack(side="left", fill="y", padx=6, pady=6)
        ttk.Button(btns, text="▶  Play Offline", style="Accent.TButton",
                   command=self.play_offline).pack(fill="x", pady=(0, 6), ipady=6)
        ttk.Button(btns, text="Play in Chromium (exact)", command=self.play_exact).pack(fill="x", pady=2)
        ttk.Button(btns, text="Stop server", command=self.stop_server).pack(fill="x", pady=2)
        ttk.Separator(btns).pack(fill="x", pady=6)
        ttk.Button(btns, text="Export package (.zip)...", command=self.export_package).pack(fill="x", pady=2)
        ttk.Button(btns, text="Export files to folder", command=self.export_files).pack(fill="x", pady=2)
        ttk.Button(btns, text="Export HAR...", command=self.export_har).pack(fill="x", pady=2)
        ttk.Button(btns, text="Open folder", command=self.open_folder).pack(fill="x", pady=2)
        ttk.Button(btns, text="Refresh", command=self.refresh_archives).pack(fill="x", pady=2)
        ttk.Separator(btns).pack(fill="x", pady=6)
        ttk.Button(btns, text="🗑  Delete", command=self.delete_archive).pack(fill="x", pady=2)
        self.tree.bind("<Delete>", lambda _e: self.delete_archive())
        self.tree.bind("<Button-3>", self._context_menu)
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Play Offline", command=self.play_offline)
        self.menu.add_command(label="Play in Chromium (exact)", command=self.play_exact)
        self.menu.add_command(label="Open folder", command=self.open_folder)
        self.menu.add_command(label="Export package (.zip)...", command=self.export_package)
        self.menu.add_separator()
        self.menu.add_command(label="Delete", command=self.delete_archive)

        logf = ttk.LabelFrame(self, text="Log")
        logf.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log_box = ScrolledText(logf, height=12, font=("Consolas", 9), state="disabled",
                                    bg="#111", fg="#d6d6d6", insertbackground="#fff")
        self.log_box.pack(fill="both", expand=True, padx=6, pady=6)

    # ------------------------------------------------------------- logging
    def log(self, msg: str) -> None:  # thread-safe
        self.log_q.put(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def _drain_log(self):
        lines = []
        while True:
            try:
                lines.append(self.log_q.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", "\n".join(lines) + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(100, self._drain_log)

    # ------------------------------------------------------------ archives
    def refresh_archives(self, select: str | None = None):
        cur = select or self.selected_name()
        self.tree.delete(*self.tree.get_children())
        for a in list_archives():
            try:
                m, s = a.meta(), a.stats()
            except Exception as e:  # corrupt / locked db
                m, s = {"start_url": f"<error: {e}>"}, {"files": 0, "bytes": 0}
            srv = self.servers.get(a.name)
            self.tree.insert("", "end", iid=a.name, text=a.name, values=(
                m.get("start_url", "(incomplete)"), s["files"], human_size(s["bytes"]),
                m.get("created_at", ""), srv.base_url if srv else ""))
        if cur and self.tree.exists(cur):
            self.tree.selection_set(cur)
            self.tree.see(cur)

    def selected_name(self) -> str | None:
        sel = self.tree.selection() if hasattr(self, "tree") else ()
        return sel[0] if sel else None

    def selected_archive(self) -> Archive | None:
        name = self.selected_name()
        if not name:
            messagebox.showinfo(APP_TITLE, "Select an archive in the list first.")
            return None
        return Archive(default_root() / name)

    # ------------------------------------------------------------- capture
    def start_capture(self):
        from replicator.capture import CaptureOptions, capture_blocking
        url = self.v_url.get().strip()
        if not url:
            messagebox.showwarning(APP_TITLE, "Enter a URL to capture.")
            return
        if self.capture_thread and self.capture_thread.is_alive():
            return
        try:
            opts = CaptureOptions(
                url=url, name=self.v_name.get(), depth=int(self.v_depth.get()),
                max_pages=int(self.v_pages.get()), wait_seconds=float(self.v_wait.get()),
                timeout_seconds=float(self.v_timeout.get()), headless=not self.v_show.get(),
                scroll=self.v_scroll.get(), max_asset_mb=float(self.v_maxmb.get()),
                workers=int(self.v_workers.get()), click_all=self.v_click.get())
        except (tk.TclError, ValueError) as e:
            messagebox.showerror(APP_TITLE, f"Invalid setting: {e}")
            return
        self.capture_stop = threading.Event()
        self.btn_capture.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.log(f"Capturing {url} ...")

        def run():
            name = None
            try:
                name = capture_blocking(opts, self.log, self.capture_stop).name
            except Exception as e:
                self.log(f"CAPTURE FAILED: {e}")
                self.log(traceback.format_exc().rstrip())
            finally:
                self.after(0, lambda: self._capture_done(name))

        self.capture_thread = threading.Thread(target=run, daemon=True)
        self.capture_thread.start()

    def _capture_done(self, name):
        self.btn_capture.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.refresh_archives(select=name)

    def stop_capture(self):
        if self.capture_stop:
            self.capture_stop.set()
            self.log("Stopping after the current page...")

    # -------------------------------------------------------------- replay
    def play_offline(self):
        from replicator.server import ReplayServer
        a = self.selected_archive()
        if not a:
            return
        srv = self.servers.get(a.name)
        if srv is None:
            try:
                srv = ReplayServer(a, log=self.log)
                srv.start()
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"Could not start server:\n{e}")
                return
            self.servers[a.name] = srv
            self.refresh_archives()
        self.log(f"Opening {srv.launch_url}  (Chrome/Edge recommended; misses are logged here)")
        webbrowser.open(srv.launch_url)

    def play_exact(self):
        from replicator.replay import replay_exact_blocking
        a = self.selected_archive()
        if not a:
            return

        def run():
            try:
                replay_exact_blocking(a, self.log)
            except Exception as e:
                self.log(f"EXACT REPLAY FAILED: {e}")

        threading.Thread(target=run, daemon=True).start()

    def stop_server(self):
        name = self.selected_name()
        srv = self.servers.pop(name, None) if name else None
        if srv:
            srv.stop()
            self.log(f"Stopped server for '{name}'.")
            self.refresh_archives()

    # --------------------------------------------------------------- misc
    def export_har(self):
        a = self.selected_archive()
        if not a:
            return
        out = filedialog.asksaveasfilename(defaultextension=".har", initialfile=f"{a.name}.har",
                                           filetypes=[("HTTP Archive", "*.har")])
        if not out:
            return

        def run():
            try:
                self.log(f"Exported HAR: {a.export_har(out)}")
            except Exception as e:
                self.log(f"HAR export failed: {e}")

        threading.Thread(target=run, daemon=True).start()

    def export_files(self):
        a = self.selected_archive()
        if not a:
            return

        def run():
            try:
                out = a.export_files(log=self.log)
                os.startfile(a.dir)
            except Exception as e:
                self.log(f"Export failed: {e}")

        threading.Thread(target=run, daemon=True).start()

    def export_package(self):
        a = self.selected_archive()
        if not a:
            return
        out = filedialog.asksaveasfilename(defaultextension=".zip", initialfile=f"{a.name}.zip",
                                           filetypes=[("Zip package", "*.zip")])
        if not out:
            return

        def run():
            try:
                self.log("Building package (files + offline server + index.html)...")
                a.export_package(out, log=self.log)
                os.startfile(os.path.dirname(out))
            except Exception as e:
                self.log(f"Package export failed: {e}")

        threading.Thread(target=run, daemon=True).start()

    def _context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)

    def delete_archive(self):
        name = self.selected_name()
        if not name:
            messagebox.showinfo(APP_TITLE, "Select an archive in the list first.")
            return
        if not messagebox.askyesno(APP_TITLE, f"Delete '{name}'?\n\nIt is moved to the Recycle Bin, "
                                              "so you can restore it from there."):
            return
        srv = self.servers.pop(name, None)
        if srv:
            srv.stop()
        try:
            how = Archive(default_root() / name).delete()
            self.log(f"Deleted '{name}' ({'moved to Recycle Bin' if how == 'recycled' else 'removed'}).")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not delete '{name}':\n{e}\n\n"
                                            "Close any window or PLAY.bat that uses it and try again.")
        self.refresh_archives()

    def open_folder(self):
        name = self.selected_name()
        os.startfile(default_root() / name if name else default_root())

    def on_close(self):
        if self.capture_stop:
            self.capture_stop.set()
        for srv in self.servers.values():
            try:
                srv.stop()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
