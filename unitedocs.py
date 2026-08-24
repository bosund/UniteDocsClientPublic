

import tkinter as tk
from tkinter import ttk
import sys
import multiprocessing
from __version__ import __version__


def _enable_dpi_awareness():
    """Gør processen DPI-aware (kun Windows) FØR Tk oprettes.

    Uden dette strækker Windows appen op med en sløret bitmap-skalering på skærme
    over 100 %. Med DPI-awareness rapporterer Tk den rigtige DPI, og vores
    vektorikoner (theme.scaling/px) tegnes skarpt i den faktiske opløsning.

    Vi bruger *system*-DPI-awareness (1), ikke per-monitor: sv-ttk's chrome er
    faste PNG-sprites uden skalering, så per-monitor ville kunne efterlade chromet
    i én skalering og vores vektorikoner i en anden på samme skærm. shcore findes
    fra Windows 8.1; user32-kaldet er en fallback for ældre versioner. Alt er
    pakket i try/except — DPI må aldrig forhindre opstart.
    """
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()   # Vista+ fallback
        except (AttributeError, OSError):
            pass


def main():
    """
    Main function to control application startup, including the splash screen.
    """
    from app.logging_config import configure_logging
    configure_logging()

    # IPC forward check — kør inden Tk initialiseres
    if sys.argv[1:]:
        import time
        from app import ipc

        result = ipc.try_forward_to_existing(sys.argv[1:])
        if result is True:
            return  # Sendt til eksisterende instans

        if result is False and not ipc.try_become_primary():
            # En anden proces vandt mutex — vent på at dens IPC-server er klar
            for _ in range(60):  # op til 30 sekunder
                time.sleep(0.5)
                r = ipc.try_forward_to_existing(sys.argv[1:])
                if r is True:
                    return
                if r is False:
                    break  # Primæren er død — åbn nyt vindue

        elif result is None:
            # En instans starter allerede (placeholder i instances.json)
            for _ in range(60):  # op til 30 sekunder
                time.sleep(0.5)
                r = ipc.try_forward_to_existing(sys.argv[1:])
                if r is True:
                    return
                if r is False:
                    break  # Primæren er død — åbn nyt vindue

        # Vi er primær — registrer placeholder straks så sekundærer ved at vi starter
        ipc.register_starting()

    # DPI-awareness SKAL sættes før det første Tk-vindue oprettes (splashen nedenfor).
    _enable_dpi_awareness()

    # --- SPLASH SCREEN SETUP ---
    splash_root = tk.Tk()
    splash_root.title("Loading")
    splash_root.overrideredirect(True) # Borderless window

    # Center the splash screen
    window_width = 400
    window_height = 150
    screen_width = splash_root.winfo_screenwidth()
    screen_height = splash_root.winfo_screenheight()
    center_x = int(screen_width/2 - window_width / 2)
    center_y = int(screen_height/2 - window_height / 2)
    splash_root.geometry(f'{window_width}x{window_height}+{center_x}+{center_y}')

    splash_frame = ttk.Frame(splash_root, padding=10)
    splash_frame.pack(expand=True, fill="both")
    ttk.Label(splash_frame, text="Unite Docs", font=("Segoe UI", 16)).pack(pady=10)
    progress_label = ttk.Label(splash_frame, text="Starter...", font=("Segoe UI", 10))
    progress_label.pack(pady=5)
    progress_bar = ttk.Progressbar(splash_frame, orient="horizontal", length=300, mode="determinate")
    progress_bar.pack(pady=10)
    splash_root.lift()
    splash_root.update()

    def load_full_app():
        """
        Handles the delayed import of heavy libraries and the main application code.
        """
        def update_splash(text, progress):
            progress_label.config(text=text)
            progress_bar['value'] = progress
            splash_root.update_idletasks()

        # --- DELAYED IMPORTS ---
        update_splash("Loading system libraries...", 25)
        from app.main_app import PDFTool

        update_splash("Preparing application...", 95)
        
        # --- LAUNCH APPLICATION ---
        splash_root.destroy()
        app = PDFTool(initial_files=sys.argv[1:])
        app.mainloop()

    splash_root.after(200, load_full_app)
    splash_root.mainloop()

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
