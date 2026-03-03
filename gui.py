import webview
import os
import subprocess
import threading
import platform 
import json
import re
import sys
import time
from pathlib import Path

# Video2X Paths and Environment
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Modified VIDEO2X_EXEC based on instruction
if platform.system() == "Windows":
    VIDEO2X_EXEC = os.path.join(BASE_DIR, "build", "video2x.exe")
else:
    VIDEO2X_EXEC = os.path.join(BASE_DIR, "build", "video2x")

HEVC_CODEC = "hevc_videotoolbox" if platform.system() == "Darwin" else "libx265" # Added HEVC_CODEC
MODEL_DIR = os.path.join(BASE_DIR, "models")
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

class LogBatcher:
    def __init__(self, window, interval=0.2):
        self.window = window
        self.interval = interval
        self.queue = []
        self.lock = threading.Lock()
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def add(self, msg):
        with self.lock:
            self.queue.append(msg)

    def _run(self):
        while self.running:
            time.sleep(self.interval)
            batch = []
            with self.lock:
                if self.queue:
                    batch = self.queue
                    self.queue = []
            
            if batch:
                try:
                    self.window.evaluate_js(f"onLogBatch({json.dumps(batch)})")
                except Exception as e:
                    debug_log(f"BATCHER ERROR: {str(e)}")

    def stop(self):
        self.running = False

class Api:
    def __init__(self):
        self.window = None
        self.process = None

    def select_file(self):
        debug_log("Opening file dialog...")
        result = self.window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False)
        debug_log(f"Dialog result: {result}")
        if result:
            path = result[0]
            filename = os.path.basename(path)
            return {"path": path, "filename": filename}
        return None

    def select_output_file(self):
        debug_log("Opening save file dialog...")
        result = self.window.create_file_dialog(webview.SAVE_DIALOG, file_types=("Video Files (*.mp4;*.mkv)", "Image Files (*.png;*.jpg)"))
        debug_log(f"Save dialog result: {result}")
        if result:
            return result
        return None

    def stop_upscale(self):
        if self.process:
            debug_log("Stopping upscale process...")
            self.process.terminate()
            self.process = None
            return True
        return False

    def run_upscale(self, params):
        debug_log(f"run_upscale called with params: {params}")
        input_path = params.get("input")
        output_path = params.get("output")
        processor = params.get("processor", "realesrgan")
        scale = params.get("scale", 2)
        libplacebo_extra = params.get("libplacebo_extra")
        use_libplacebo_hdr = params.get("use_libplacebo_hdr", False)
        codec = params.get("codec", "libx264")
        pix_fmt = params.get("pix_fmt")
        quality = params.get("quality")

        if not input_path:
            debug_log("Input path not provided, aborting upscale.")
            return False

        # Determine output path
        if not output_path:
            input_file = Path(input_path)
            output_path = str(input_file.with_name(f"{input_file.stem}_upscaled{input_file.suffix}"))
        else:
            # Ensure output has an extension so FFmpeg doesn't fail
            if not Path(output_path).suffix:
                output_path += ".mp4"

        # macOS HDR: two-pass with CPU color processing + hardware HEVC 10-bit
        is_macos_hdr = (use_libplacebo_hdr and platform.system() == "Darwin"
                        and processor in ("realcugan", "realesrgan"))

        if is_macos_hdr:
            self._run_two_pass_hdr_cpu(input_path, output_path, processor, scale,
                                       libplacebo_extra, codec, pix_fmt, quality)
        else:
            cmd = self._build_single_pass_cmd(input_path, output_path, processor, scale,
                                              libplacebo_extra, use_libplacebo_hdr,
                                              codec, pix_fmt, quality)
            self._execute_pass(cmd, processor, "")
        return True

    def _build_single_pass_cmd(self, input_path, output_path, processor, scale,
                               libplacebo_extra, use_libplacebo_hdr,
                               codec, pix_fmt, quality):
        """Build a video2x command for a single-pass run."""
        cmd = [VIDEO2X_EXEC, "-i", input_path, "-o", output_path]

        extension = Path(output_path).suffix.lower().strip('.')
        cmd.extend(["-p", processor if processor != "anime4k" else "libplacebo"])

        # Encoder options
        cmd.extend(["-c", codec])
        if codec == "hevc_videotoolbox":
            cmd.extend(["-e", "allow_sw=1"])
            if quality is not None and quality != -1:
                try:
                    q_val = float(quality)
                    bitrate_mb = max(1, int(50 - (q_val / 51.0) * 49))
                    cmd.extend(["-e", f"b={bitrate_mb}M"])
                except ValueError:
                    pass
        else:
            if quality is not None and quality != -1:
                cmd.extend(["--quality", str(quality)])

        if pix_fmt:
            cmd.extend(["--pix-fmt", pix_fmt])
        if use_libplacebo_hdr:
            # On non-macOS, single-pass HDR via libplacebo Vulkan works fine
            cmd.append("--libplacebo-hdr")

        # Advanced libplacebo options
        if processor == "anime4k" and libplacebo_extra:
            cmd.extend(["--libplacebo-extra", libplacebo_extra])

        # MP4 subtitle issue on macOS
        if extension == "mp4":
            cmd.append("--no-copy-subtitles")

        # Processor-specific arguments
        if processor == "rife":
            cmd += ["-m", str(scale), "--rife-model", "rife-v4.6"]
        elif processor == "anime4k":
            try:
                size_out = subprocess.check_output(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
                     input_path], text=True, stderr=subprocess.STDOUT).strip()
                w_str, h_str = size_out.split('x')
                width = int(w_str) * scale
                height = int(h_str) * scale
                cmd += ["-w", str(width), "-h", str(height), "--libplacebo-shader", "anime4k-v4-a"]
            except Exception as e:
                debug_log(f"FFPROBE ERROR: {str(e)}")
                return None
        elif processor == "realesrgan":
            cmd += ["-s", str(scale), "--realesrgan-model", "realesr-animevideov3"]
        elif processor == "realcugan":
            if scale <= 2:
                cmd += ["-s", str(scale), "--realcugan-model", "models-se", "--realcugan-syncgap", "3"]
            else:
                # syncgap=3 degrades 4x upscaling quality significantly, and video2x C++ defaults to 3.
                # Explicitly override with 0 to fix severe pixelation at 4x scale.
                cmd += ["-s", str(scale), "--realcugan-model", "models-se", "--realcugan-syncgap", "0"]
        else:
            cmd += ["-s", str(scale)]

        cmd += ["--log-level", "debug"]
        return cmd

    def _run_two_pass_hdr_cpu(self, input_path, output_path, processor, scale,
                              libplacebo_extra, codec, pix_fmt, quality):
        """Two-pass HDR for macOS: Pass 1 = AI upscale, Pass 2 = FFmpeg CPU color → HEVC 10-bit HW."""
        debug_log("Starting two-pass HDR pipeline (CPU color + HW encode)")
        self.window.evaluate_js(f"onLog({json.dumps('[info] macOS HDR: two-pass pipeline (AI upscale → CPU color → HEVC 10-bit HW encode)')})")

        # Temp file for pass 1 (lossless intermediate)
        input_file = Path(input_path)
        temp_path = str(input_file.with_name(f"{input_file.stem}_pass1_tmp.mkv"))

        def two_pass_runner():
            # ── Pass 1: AI upscale only (no HDR) ──
            self.window.evaluate_js(f"onLog({json.dumps('[info] ═══ Pass 1/2: AI Upscale ═══')})")
            pass1_cmd = self._build_single_pass_cmd(
                input_path, temp_path, processor, scale,
                libplacebo_extra, use_libplacebo_hdr=False,
                codec="libx264", pix_fmt=None, quality=0  # near-lossless intermediate
            )
            if pass1_cmd is None:
                self.window.evaluate_js(f"onLog({json.dumps('[error] Failed to build Pass 1 command')})")
                self.window.evaluate_js("onFinished(false)")
                return

            pass1_env = os.environ.copy()
            pass1_env["VK_ENABLE_PORTABILITY_ENUMERATION"] = "1"
            pass1_env["VK_ICD_FILENAMES"] = ICD_FILE
            current_dyld = pass1_env.get("DYLD_LIBRARY_PATH", "")
            pass1_env["DYLD_LIBRARY_PATH"] = f"{LIB_DIR}:/usr/local/lib:{current_dyld}"

            command_str = ' '.join(pass1_cmd)
            debug_log(f"[Pass 1/2] Executing: {command_str}")
            self.window.evaluate_js(f"onLog({json.dumps(f'[Pass 1/2] [info] Command: {command_str}')})")
            self.window.evaluate_js(f"onLog({json.dumps('[Pass 1/2] [info] Loading AI models... this may take 30-60 seconds.')})")

            pass1_ok = self._run_process(pass1_cmd, pass1_env, processor, "[Pass 1/2] ")
            if not pass1_ok:
                self.window.evaluate_js(f"onLog({json.dumps('[error] Pass 1 (AI upscale) failed.')})")
                self.window.evaluate_js("onFinished(false)")
                self._cleanup_temp(temp_path)
                return

            # ── Pass 2: FFmpeg CPU color processing → HEVC 10-bit hardware encode ──
            self.window.evaluate_js(f"onLog({json.dumps('[info] ═══ Pass 2/2: HDR Color → HEVC 10-bit ═══')})")

            # Build FFmpeg command for CPU-based HDR color conversion + HW encode
            pass2_cmd = [
                "ffmpeg", "-y",
                "-i", temp_path,
                "-vf", "colorspace=all=bt2020:iall=bt709:fast=1,format=p010le",
                "-c:v", "hevc_videotoolbox",
                "-allow_sw", "1",
                "-tag:v", "hvc1",
                "-c:a", "copy",
            ]

            # Apply quality/bitrate settings for final output
            if quality is not None and quality != -1:
                try:
                    q_val = float(quality)
                    bitrate_mb = max(1, int(50 - (q_val / 51.0) * 49))
                    pass2_cmd.extend(["-b:v", f"{bitrate_mb}M"])
                except ValueError:
                    pass

            pass2_cmd.append(output_path)

            pass2_env = os.environ.copy()
            command_str = ' '.join(pass2_cmd)
            debug_log(f"[Pass 2/2] Executing: {command_str}")
            self.window.evaluate_js(f"onLog({json.dumps(f'[Pass 2/2] [info] Command: {command_str}')})")
            self.window.evaluate_js(f"onLog({json.dumps('[Pass 2/2] [info] CPU color processing + hardware HEVC 10-bit encoding...')})")

            pass2_ok = self._run_ffmpeg_hdr_pass(pass2_cmd, pass2_env)

            # ── Cleanup ──
            self._cleanup_temp(temp_path)

            if pass2_ok:
                self.window.evaluate_js(f"onLog({json.dumps('[info] ✓ HDR processing complete! Output: HEVC 10-bit BT.2020')})")
            else:
                self.window.evaluate_js(f"onLog({json.dumps('[error] Pass 2 (HDR color/encode) failed.')})")
            self.window.evaluate_js(f"onFinished({json.dumps(pass2_ok)})")

        threading.Thread(target=two_pass_runner, daemon=True).start()

    def _run_ffmpeg_hdr_pass(self, cmd, env):
        """Run an FFmpeg command for HDR pass 2. Returns True on success."""
        batcher = LogBatcher(self.window)
        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                cwd=BASE_DIR
            )
        except Exception as e:
            debug_log(f"FFMPEG SUBPROCESS ERROR: {str(e)}")
            batcher.add(f"[Pass 2/2] [error] Failed to start FFmpeg: {str(e)}")
            batcher.stop()
            return False

        if self.process and self.process.stdout:
            for line in self.process.stdout:
                clean_line = line.strip()
                if not clean_line:
                    continue
                debug_log(f"STDOUT: {clean_line}")
                # FFmpeg progress lines (frame=, fps=, etc.)
                if clean_line.startswith("frame=") or "speed=" in clean_line:
                    batcher.add(f"[Pass 2/2] {clean_line}")
                elif "[error]" in clean_line.lower() or "Error" in clean_line:
                    batcher.add(f"[Pass 2/2] [error] {clean_line}")

        if self.process:
            self.process.wait()
            return_code = self.process.returncode
            self.process = None
        else:
            return_code = -1

        time.sleep(0.3)
        batcher.stop()
        debug_log(f"[Pass 2/2] FFmpeg finished with code: {return_code}")
        return return_code == 0

    def _cleanup_temp(self, temp_path):
        """Remove temporary intermediate file."""
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
                debug_log(f"Cleaned up temp file: {temp_path}")
        except Exception as e:
            debug_log(f"Failed to clean up temp file: {e}")

    def _execute_pass(self, cmd, processor, log_prefix):
        """Execute a single pass asynchronously (fire-and-forget thread)."""
        if cmd is None:
            self.window.evaluate_js(f"onLog({json.dumps('[error] Failed to build command.')})")
            self.window.evaluate_js("onFinished(false)")
            return

        env = os.environ.copy()
        env["VK_ENABLE_PORTABILITY_ENUMERATION"] = "1"
        env["VK_ICD_FILENAMES"] = ICD_FILE
        current_dyld = env.get("DYLD_LIBRARY_PATH", "")
        env["DYLD_LIBRARY_PATH"] = f"{LIB_DIR}:/usr/local/lib:{current_dyld}"

        command_str = ' '.join(cmd)
        debug_log(f"{log_prefix}Executing command: {command_str}")
        self.window.evaluate_js(f"onLog({json.dumps(f'{log_prefix}[info] Command: {command_str}')})")
        self.window.evaluate_js(f"onLog({json.dumps(f'{log_prefix}[info] Loading AI models and initializing Vulkan... this may take 30-60 seconds.')})")

        def runner():
            return self._run_process(cmd, env, processor, log_prefix)

        threading.Thread(target=runner, daemon=True).start()

    def _run_process(self, cmd, env, processor, log_prefix):
        """Core process runner. Returns True on success."""
        last_progress_time = time.time()
        last_ui_update_time = 0
        last_frame = -1
        batcher = LogBatcher(self.window)

        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                cwd=BASE_DIR
            )
        except Exception as e:
            debug_log(f"SUBPROCESS ERROR: {str(e)}")
            batcher.add(f"{log_prefix}[error] Failed to start subprocess: {str(e)}")
            batcher.stop()
            if not log_prefix:  # only for single-pass mode
                self.window.evaluate_js(f"onFinished(false)")
            return False

        if self.process and self.process.stdout:
            for line in self.process.stdout:
                clean_line = line.strip()
                if not clean_line:
                    continue

                debug_log(f"STDOUT: {clean_line}")
                batcher.add(f"{log_prefix}{clean_line}" if log_prefix else clean_line)

                progress_match = re.search(r"frame=(\d+)/(\d+)", clean_line)
                if progress_match:
                    current_frame = int(progress_match.group(1))
                    if current_frame > last_frame:
                        last_frame = current_frame
                        last_progress_time = time.time()

                    detailed_match = re.search(
                        r"frame=(\d+)/(\d+)\s*\(([\d.]+)%\);\s*fps=([\d.]+);\s*elapsed=([\d:]+);\s*remaining=([\d:]+)",
                        clean_line
                    )
                    if detailed_match:
                        now = time.time()
                        if now - last_ui_update_time > 0.5:
                            data = {
                                "percent": float(detailed_match.group(3)),
                                "fps": detailed_match.group(4),
                                "elapsed": detailed_match.group(5),
                                "remaining": detailed_match.group(6)
                            }
                            self.window.evaluate_js(f"updateProgress({json.dumps(data)})")
                            last_ui_update_time = now

                # Watchdog: detect stalls
                if time.time() - last_progress_time > 45:
                    if processor == "anime4k":
                        debug_log("WATCHDOG: Anime4k video stall detected.")
                        batcher.add(f"{log_prefix}[error] DETECTED STALL: Anime4k is not progressing. Known issue on macOS.")
                        batcher.add(f"{log_prefix}[tip] PLEASE STOP and use Real-CUGAN for stable video upscaling.")
                        last_progress_time = time.time() + 3600

        if self.process:
            self.process.wait()
            return_code = self.process.returncode
            self.process = None
        else:
            return_code = -1

        time.sleep(0.5)
        batcher.stop()

        debug_log(f"{log_prefix}Process finished with code: {return_code}")
        if return_code != 0 and return_code != -1:
            log_content = open(DEBUG_LOG).read()
            if "Filter 'libplacebo' not found" in log_content:
                self.window.evaluate_js(f"onLog({json.dumps(f'{log_prefix}[error] FFmpeg does not support libplacebo (Anime4k).')})")
                self.window.evaluate_js(f"onLog({json.dumps(f'{log_prefix}[tip] Use Real-CUGAN or Real-ESRGAN instead.')})")
            else:
                self.window.evaluate_js(f"onLog({json.dumps(f'{log_prefix}[error] Process exited with code {return_code}')})")

        # For single-pass (no prefix), emit onFinished here
        if not log_prefix:
            self.window.evaluate_js(f"onFinished({json.dumps(return_code == 0)})")

        return return_code == 0

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
