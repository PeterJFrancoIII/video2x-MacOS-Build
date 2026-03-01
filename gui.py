import webview
import os
import subprocess
import threading
import json
import re
import sys
import time

# Video2X Paths and Environment
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VIDEO2X_EXEC = os.path.join(BASE_DIR, "build", "video2x-install", "bin", "video2x")
MODEL_DIR = os.path.join(BASE_DIR, "build", "models")
LIB_DIR = os.path.join(BASE_DIR, "build", "video2x-install", "lib")
ICD_FILE = "/usr/local/etc/vulkan/icd.d/MoltenVK_icd.json"
DEBUG_LOG = os.path.join(BASE_DIR, "gui_debug.log")

def debug_log(msg):
    with open(DEBUG_LOG, "a") as f:
        f.write(f"[{time.ctime()}] {msg}\n")
    print(f"DEBUG: {msg}")
    sys.stdout.flush()

# Clear log on start
with open(DEBUG_LOG, "w") as f:
    f.write(f"--- Video2X GUI Debug Log Start ---\n")

class Api:
    def __init__(self):
        self.window = None

    def select_file(self):
        debug_log("Opening file dialog...")
        result = self.window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False)
        debug_log(f"Dialog result: {result}")
        if result:
            path = result[0]
            filename = os.path.basename(path)
            return {"path": path, "filename": filename}
        return None

    def run_upscale(self, params):
        debug_log(f"run_upscale called with params: {params}")
        input_path = params.get("input")
        processor = params.get("processor", "realesrgan")
        scale = params.get("scale", 2)
        extension = params.get("extension", "png")

        # Determine output path
        base, _ = os.path.splitext(input_path)
        output_path = f"{base}_upscaled.{extension}"

        # Standard Video2X command
        cmd = [
            VIDEO2X_EXEC,
            "-i", input_path,
            "-o", output_path,
            "-p", processor if processor != "anime4k" else "libplacebo",
        ]

        # Fix for MP4 subtitle compatibility
        if extension.lower() == "mp4":
            cmd.append("--no-copy-streams")

        # Handle processor-specific arguments
        if processor == "rife":
            cmd += ["-m", str(scale), "--rife-model", "rife-v4.6"]
        elif processor == "anime4k":
            try:
                ffprobe_cmd = [
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
                    input_path
                ]
                debug_log(f"Running ffprobe: {' '.join(ffprobe_cmd)}")
                self.window.evaluate_js(f"onLog({json.dumps(f'[info] Running ffprobe to detect size...')})")
                size_out = subprocess.check_output(ffprobe_cmd, text=True, stderr=subprocess.STDOUT).strip()
                debug_log(f"ffprobe output: {size_out}")
                w_str, h_str = size_out.split('x')
                width = int(w_str) * scale
                height = int(h_str) * scale
                cmd += ["-w", str(width), "-h", str(height), "--libplacebo-shader", "anime4k-v4-a"]
            except Exception as e:
                debug_log(f"FFPROBE ERROR: {str(e)}")
                self.window.evaluate_js(f"onLog({json.dumps(f'[error] Failed to get input size for Anime4k: {str(e)}')})")
                return False
        elif processor == "realesrgan":
            cmd += ["-s", str(scale), "--realesrgan-model", "realesr-animevideov3"]
        elif processor == "realcugan":
            cmd += ["-s", str(scale), "--realcugan-model", "models-se"]
        else:
            cmd += ["-s", str(scale)]

        # Environment variables for macOS
        env = os.environ.copy()
        env["VK_ENABLE_PORTABILITY_ENUMERATION"] = "1"
        env["VK_ICD_FILENAMES"] = ICD_FILE
        current_dyld = env.get("DYLD_LIBRARY_PATH", "")
        env["DYLD_LIBRARY_PATH"] = f"{LIB_DIR}:/usr/local/lib:{current_dyld}"

        command_str = ' '.join(cmd)
        debug_log(f"Executing command: {command_str}")
        self.window.evaluate_js(f"onLog({json.dumps(f'[info] Command: {command_str}')})")

        def runner():
            last_progress_time = time.time()
            last_frame = -1
            
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                    cwd=os.path.dirname(VIDEO2X_EXEC)
                )

                if process.stdout:
                    for line in process.stdout:
                        clean_line = line.strip()
                        debug_log(f"STDOUT: {clean_line}")
                        self.window.evaluate_js(f"onLog({json.dumps(clean_line)})")
                        
                        progress_match = re.search(r"frame=(\d+)/(\d+)", clean_line)
                        if progress_match:
                            current_frame = int(progress_match.group(1))
                            if current_frame > last_frame:
                                last_frame = current_frame
                                last_progress_time = time.time()

                            # Detailed progress for the UI
                            detailed_match = re.search(r"frame=(\d+)/(\d+)\s*\(([\d.]+)%\);\s*fps=([\d.]+);\s*elapsed=([\d:]+);\s*remaining=([\d:]+)", clean_line)
                            if detailed_match:
                                data = {
                                    "percent": float(detailed_match.group(3)),
                                    "fps": detailed_match.group(4),
                                    "elapsed": detailed_match.group(5),
                                    "remaining": detailed_match.group(6)
                                }
                                self.window.evaluate_js(f"updateProgress({json.dumps(data)})")

                        # Watchdog check (every few lines or so)
                        if time.time() - last_progress_time > 45: # 45 seconds without frame progress
                            if processor == "anime4k":
                                debug_log("WATCHDOG: Anime4k video stall detected.")
                                self.window.evaluate_js(f"onLog({json.dumps('[error] DETECTED STALL: Anime4k is not progressing. This is a known issue with libplacebo on macOS.')})")
                                self.window.evaluate_js(f"onLog({json.dumps('[tip] PLEASE STOP and use Real-CUGAN for stable video upscaling.')})")
                                # We don't kill it automatically to allow the user to see the logs, 
                                # but we stop the watchdog so we don't spam.
                                last_progress_time = time.time() + 3600 

                process.wait()
                debug_log(f"Process finished with code: {process.returncode}")
                if process.returncode != 0:
                    # Detect common FFmpeg/libplacebo errors
                    log_content = open(DEBUG_LOG).read()
                    if "Filter 'libplacebo' not found" in log_content:
                         self.window.evaluate_js(f"onLog({json.dumps('[error] SYSTEM LIMITATION: Your FFmpeg does not support libplacebo (Anime4k).')})")
                         self.window.evaluate_js(f"onLog({json.dumps('[tip] PLEASE USE: Real-CUGAN or Real-ESRGAN for high-quality anime upscaling.')})")
                    else:
                        self.window.evaluate_js(f"onLog({json.dumps(f'[error] Process exited with code {process.returncode}')})")
                return process.returncode == 0
            except Exception as e:
                debug_log(f"RUNNER EXCEPTION: {str(e)}")
                self.window.evaluate_js(f"onLog({json.dumps(f'[error] Runner exception: {str(e)}')})")
                return False

        threading.Thread(target=runner, daemon=True).start()
        return True

if __name__ == "__main__":
    api = Api()
    html_file = os.path.join(BASE_DIR, "gui_assets", "index.html")
    
    # Check if we have the assets
    if not os.path.exists(html_file):
        print(f"Error: GUI assets not found at {html_file}")
        sys.exit(1)

    window = webview.create_window(
        "Video2X - Premium GUI",
        url=html_file,
        width=1100,
        height=750,
        background_color='#121212',
        js_api=api
    )
    api.window = window
    webview.start(debug=True)
