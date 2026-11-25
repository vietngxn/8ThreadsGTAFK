"""
Ghi âm âm thanh GTA V (qua VB-Cable), nhận diện tiếng chuông điện thoại trong game
bằng cách so khớp với mẫu bạn tự thu, và gửi cảnh báo qua Telegram.
"""
import json
import os
import queue
import shutil
import threading
import time
import urllib.parse
import urllib.request
import wave

import numpy as np

try:
    import sounddevice as sd
except Exception:  # thiếu thư viện hoặc PortAudio
    sd = None

SR = 16000
BLOCK = SR // 10  # 0,1 giây / khối
DATA_DIR = os.path.join(os.environ.get("APPDATA", "."), "GTAEco")
CLIPS_DIR = os.path.join(DATA_DIR, "clips")
SAMPLES_DIR = os.path.join(DATA_DIR, "samples")
RAID_TYPES = ["Chuong"]  # chỉ nhận diện tiếng chuông điện thoại


# ---------------------------------------------------------------- Telegram
def _call(token, method, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode() if params else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
        return json.load(r)


def tg_send(token, chat_id, text):
    try:
        res = _call(token, "sendMessage", {"chat_id": chat_id, "text": text})
        return bool(res.get("ok")), ""
    except Exception as e:
        return False, str(e)


def tg_find_chat_id(token):
    try:
        res = _call(token, "getUpdates")
        for u in reversed(res.get("result", [])):
            m = u.get("message") or u.get("channel_post") or {}
            if "chat" in m:
                return str(m["chat"]["id"]), ""
        return None, "Chưa có tin nhắn nào gửi cho bot. Hãy nhắn 1 tin cho bot rồi thử lại."
    except Exception as e:
        return None, str(e)


# ------------------------------------------------------------- file / thiết bị
def write_wav(path, x):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def read_wav(path):
    with wave.open(path, "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype("float32") / 32768


def list_inputs():
    if sd is None:
        return []
    names = []
    try:
        for d in sd.query_devices():
            if d["max_input_channels"] > 0 and d["name"] not in names:
                names.append(d["name"])
    except Exception:
        pass
    return names


def find_device(name):
    key = name.lower()[:20]
    apis = sd.query_hostapis()
    cands = [(i, d) for i, d in enumerate(sd.query_devices())
             if d["max_input_channels"] > 0 and key in d["name"].lower()]
    for i, d in cands:  # ưu tiên MME vì tự đổi tần số lấy mẫu
        if apis[d["hostapi"]]["name"] == "MME":
            return i
    return cands[0][0] if cands else None


def list_clips():
    os.makedirs(CLIPS_DIR, exist_ok=True)
    files = [os.path.join(CLIPS_DIR, f) for f in os.listdir(CLIPS_DIR) if f.endswith(".wav")]
    return sorted(files, reverse=True)


# --------------------------------------------------------- nhận diện lời thoại
def features(x):
    """Phổ log chia 24 dải tần: đủ để so khớp một đoạn thoại ghi sẵn."""
    n_fft, hop = 512, 160
    if len(x) < n_fft:
        return np.zeros((0, 1))
    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft)[::hop]
    spec = np.abs(np.fft.rfft(frames * np.hanning(n_fft), axis=1)) ** 2
    edges = np.unique(np.geomspace(3, 256, 25).astype(int))
    bands = np.stack([spec[:, a:b].sum(1) for a, b in zip(edges[:-1], edges[1:])], 1)
    return np.log(bands + 1e-9)


def best_score(A, T):
    """Độ giống lớn nhất (cosine) khi trượt mẫu T dọc theo đoạn A."""
    tn = len(T)
    if tn == 0 or len(A) == 0:
        return 0.0
    if len(A) < tn:
        A = np.vstack([A, np.zeros((tn - len(A), A.shape[1]))])
    t = T - T.mean(0)
    tnorm = np.linalg.norm(t) + 1e-9
    best = 0.0
    for i in range(0, len(A) - tn + 1, 4):
        w = A[i:i + tn]
        w = w - w.mean(0)
        s = float((t * w).sum() / (tnorm * (np.linalg.norm(w) + 1e-9)))
        best = max(best, s)
    return best


# ---------------------------------------------------------------- bộ ghi âm
class Recorder(threading.Thread):
    def __init__(self, cfg, log, on_raid, on_clip):
        super().__init__(daemon=True)
        self.cfg, self.log, self.on_raid, self.on_clip = cfg, log, on_raid, on_clip
        self.q = queue.Queue()
        self.stream = None
        self.rate = SR
        self.level = 0.0
        self.seg, self.silence = [], 0
        self.test_until = 0.0
        self.retry_at = 0.0
        self.last_alert = {}
        self.templates = []
        os.makedirs(CLIPS_DIR, exist_ok=True)
        os.makedirs(SAMPLES_DIR, exist_ok=True)
        self.load_templates()

    # -- mẫu
    def load_templates(self):
        self.templates = []
        for f in sorted(os.listdir(SAMPLES_DIR)):
            if f.endswith(".wav") and f.split("_")[0] in RAID_TYPES:
                feat = features(read_wav(os.path.join(SAMPLES_DIR, f)))
                if len(feat) >= 20:
                    self.templates.append((f.split("_")[0], feat))

    def make_sample(self, clip_path, label):
        name = f"{label}_{time.strftime('%Y%m%d_%H%M%S')}.wav"
        shutil.copy(clip_path, os.path.join(SAMPLES_DIR, name))
        self.load_templates()

    # -- vòng lặp
    def wanted(self):
        return (self.cfg["enabled"] and self.cfg["rec_enabled"]) or time.time() < self.test_until

    def run(self):
        while True:
            try:
                want = self.wanted()
                if want and self.stream is None and time.time() >= self.retry_at:
                    self.open()
                elif not want and self.stream is not None:
                    self.close()
                if self.stream is not None:
                    self.process()
                else:
                    self.level = 0.0
                    time.sleep(1)
            except Exception as e:
                self.log(f"Lỗi ghi âm: {e}")
                self.close()
                self.retry_at = time.time() + 10
                time.sleep(1)

    def _cb(self, indata, frames, t, status):
        x = indata[:, 0].copy()
        if self.rate != SR:
            x = np.interp(np.linspace(0, len(x) - 1, BLOCK), np.arange(len(x)), x).astype("float32")
        self.q.put(x)

    def open(self):
        if sd is None:
            self.retry_at = time.time() + 30
            raise RuntimeError("Thiếu thư viện sounddevice (pip install sounddevice).")
        idx = find_device(self.cfg["audio_device"])
        if idx is None:
            self.retry_at = time.time() + 10
            self.log(f"Không thấy thiết bị '{self.cfg['audio_device']}'. Đã cài VB-Cable chưa?")
            return
        try:
            self.rate = SR
            self.stream = sd.InputStream(device=idx, channels=1, samplerate=SR, blocksize=BLOCK,
                                         dtype="float32", callback=self._cb)
        except Exception:
            self.rate = int(sd.query_devices(idx)["default_samplerate"])
            self.stream = sd.InputStream(device=idx, channels=1, samplerate=self.rate,
                                         blocksize=int(self.rate * 0.1), dtype="float32",
                                         callback=self._cb)
        self.stream.start()
        self.seg, self.silence = [], 0
        self.log("Bắt đầu nghe âm thanh game.")

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
            self.log("Đã dừng nghe âm thanh.")
        self.seg, self.silence = [], 0
        self.level = 0.0

    def process(self):
        try:
            blk = self.q.get(timeout=0.5)
        except queue.Empty:
            return
        rms = float(np.sqrt(np.mean(blk ** 2)))
        self.level = rms
        if rms > self.cfg["audio_threshold"]:
            self.seg.append(blk)
            self.silence = 0
        elif self.seg:
            self.seg.append(blk)
            self.silence += 1
            if self.silence >= 12:  # 1,2 giây im lặng = hết một đoạn
                self.finish()
        if len(self.seg) > 300:  # tối đa 30 giây / đoạn
            self.finish()

    def finish(self):
        blocks, trail = self.seg, self.silence
        self.seg, self.silence = [], 0
        if trail:
            blocks = blocks[:-trail]
        if len(blocks) < 3:  # ngắn hơn 0,3 giây thì bỏ
            return
        audio = np.concatenate(blocks)
        name = time.strftime("%Y%m%d_%H%M%S") + ".wav"
        write_wav(os.path.join(CLIPS_DIR, name), audio)
        self.cleanup()
        self.on_clip()
        self.check_raid(audio, name)

    def check_raid(self, audio, name):
        if not self.templates:
            return
        feat = features(audio)
        label, best = None, 0.0
        for lb, t in self.templates:
            s = best_score(feat, t)
            if s > best:
                label, best = lb, s
        if best >= 0.4:
            self.log(f"{name}: giống tiếng chuông {best:.2f}")
        if best >= self.cfg["match_threshold"] and time.time() - self.last_alert.get(label, 0) > 60:
            self.last_alert[label] = time.time()
            self.on_raid(label, best)

    def cleanup(self):
        limit = time.time() - self.cfg["keep_days"] * 86400
        for p in list_clips():
            try:
                if os.path.getmtime(p) < limit:
                    os.remove(p)
            except OSError:
                pass
