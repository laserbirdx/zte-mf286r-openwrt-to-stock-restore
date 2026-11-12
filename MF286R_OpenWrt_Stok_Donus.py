import os
import sys
import time
import struct
import shutil
import tempfile
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import paramiko


def find_7z_binary():
    # Check bundled PyInstaller path first
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = os.path.join(sys._MEIPASS, "7z.exe")
        if os.path.exists(bundled):
            return bundled
    # Check standard paths
    candidates = [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
        "7z.exe",
    ]
    for c in candidates:
        if shutil.which(c) or os.path.exists(c):
            return c
    return None


def extract_root_uimage(input_path, log_fn):
    """
    Accepts either a raw 37 MB root_uImage file or a ZTE installer .exe file
    and returns the 38,797,312-byte root_uImage bytes.
    """
    size = os.path.getsize(input_path)
    if size == 38797312:
        log_fn(f"Doğrudan root_uImage dosyası okundu ({size:,} bayt).")
        with open(input_path, "rb") as f:
            return f.read()

    seven_zip = find_7z_binary()
    if not seven_zip:
        raise RuntimeError("7z.exe bulunamadı! Lütfen 7-Zip kurun veya doğrudan root_uImage seçin.")

    log_fn(f"Kurulum paketinden Data\\version\\root_uImage çıkartılıyor: {os.path.basename(input_path)}...")
    tmp_dir = tempfile.mkdtemp(prefix="mf286r_stock_")
    try:
        # Check if HaoZip SFX stub is at 559104
        with open(input_path, "rb") as f:
            header = f.read(600000)
        seven_z_sig = b"7z\xbc\xaf\x27\x1c"
        archive_to_extract = input_path
        if len(header) > 559110 and header[559104:559110] == seven_z_sig:
            archive_to_extract = os.path.join(tmp_dir, "payload.7z")
            with open(input_path, "rb") as src, open(archive_to_extract, "wb") as dst:
                src.seek(559104)
                remaining = size - 559104 - 147
                while remaining > 0:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    dst.write(chunk)
                    remaining -= len(chunk)

        cmd = [seven_zip, "e", archive_to_extract, r"Data\version\root_uImage", f"-o{tmp_dir}", "-y"]
        CREATE_NO_WINDOW = 0x08000000
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        extracted_file = os.path.join(tmp_dir, "root_uImage")
        if res.returncode != 0 or not os.path.exists(extracted_file):
            raise RuntimeError("Seçilen .exe dosyasından root_uImage çıkartılamadı!")

        with open(extracted_file, "rb") as f:
            data = f.read()
        if len(data) != 38797312:
            raise RuntimeError(f"Geçersiz root_uImage boyutu: {len(data)} bayt (beklenen: 38,797,312).")
        log_fn("root_uImage başarıyla çıkartıldı (37 MB).")
        return data
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def connect_ssh(host, port, username, password, log_fn):
    log_fn(f"{host}:{port} adresine SSH ile bağlanılıyor (Kullanıcı: {username})...")
    sock = paramiko.Transport((host, port))
    sock.connect()
    try:
        if not password:
            try:
                sock.auth_none(username)
                log_fn("SSH bağlantısı (şifresiz / root) başarılı.")
                return sock
            except paramiko.BadAuthenticationType:
                pass
        sock.auth_password(username, password)
        log_fn("SSH bağlantısı (şifreli) başarılı.")
        return sock
    except Exception as e:
        sock.close()
        raise RuntimeError(f"SSH kimlik doğrulama hatası: {e}")


def run_ssh_cmd(transport, cmd, timeout=120):
    chan = transport.open_session()
    chan.settimeout(timeout)
    chan.exec_command(cmd)
    out = b""
    err = b""
    while True:
        if chan.recv_ready():
            out += chan.recv(4096)
        if chan.recv_stderr_ready():
            err += chan.recv_stderr(4096)
        if chan.exit_status_ready():
            while chan.recv_ready():
                out += chan.recv(4096)
            while chan.recv_stderr_ready():
                err += chan.recv_stderr(4096)
            break
        time.sleep(0.05)
    status = chan.recv_exit_status()
    chan.close()
    return status, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


def upload_bytes_via_ssh(transport, remote_path, data, log_fn, progress_cb=None):
    total = len(data)
    chan = transport.open_session()
    chan.exec_command(f"cat > {remote_path}")
    sent = 0
    chunk_size = 65536
    while sent < total:
        chunk = data[sent : sent + chunk_size]
        chan.sendall(chunk)
        sent += len(chunk)
        if progress_cb:
            progress_cb(sent, total)
    chan.shutdown_write()
    chan.recv_exit_status()
    chan.close()


def restore_stock_firmware(host, username, password, exe_or_uimage_path, log_fn, set_progress):
    set_progress(2)
    uimage = extract_root_uimage(exe_or_uimage_path, log_fn)

    # Slice 37 MB root_uImage into the 3 OpenWrt MTD partitions:
    # 1. web_8m:    0x000000 .. 0x800000 (8 MB -> NAND 0x1000000..0x1800000, inside ubiconcat0)
    # 2. kernel_4m: 0x800000 .. 0xC00000 (4 MB -> NAND 0x1800000..0x1c00000 = kernel partition)
    # 3. rootfs_25m:0xC00000 .. 0x2500000 (25 MB -> NAND 0x1c00000..0x3500000, inside ubiconcat1)
    web_8m = uimage[0x000000:0x800000]
    kernel_4m = uimage[0x800000:0xC00000]
    rootfs_25m = uimage[0xC00000:0x2500000]

    # Verify kernel uImage magic (0x27051956)
    if kernel_4m[:4] != b"\x27\x05\x19\x56":
        raise RuntimeError("Seçilen stok yazılımda geçerli bir Kernel uImage başlığı bulunamadı!")

    set_progress(10)
    transport = connect_ssh(host, 22, username, password, log_fn)
    try:
        rc, mtd_out, _ = run_ssh_cmd(transport, "cat /proc/mtd")
        log_fn("Cihazdaki MTD bölümleri:\n" + mtd_out.strip())
        if "ubiconcat0" not in mtd_out or "ubiconcat1" not in mtd_out:
            raise RuntimeError("Cihazda OpenWrt MTD bölümleri (ubiconcat0 / ubiconcat1) bulunamadı! Cihaz zaten stok yazılımda olabilir.")

        # Parse ubiconcat0 size to determine if it starts at 0x1000000 (size 00800000) or 0x1a0000 (size 01660000)
        ubiconcat0_size = 0x800000
        for line in mtd_out.splitlines():
            if '"ubiconcat0"' in line:
                parts = line.split()
                ubiconcat0_size = int(parts[1], 16)
                break

        if ubiconcat0_size == 0x800000:
            web_seek_offset = 0
            log_fn("NVRAM korumalı OpenWrt düzeni tespit edildi (ubiconcat0 = 8 MB, ofset = 0x0).")
        elif ubiconcat0_size == 0x1660000:
            web_seek_offset = 0xE60000
            log_fn("Standart OpenWrt düzeni tespit edildi (ubiconcat0 = 22.375 MB, web ofseti = 0xE60000).")
        else:
            raise RuntimeError(f"Beklenmeyen ubiconcat0 boyutu: {hex(ubiconcat0_size)}")

        # Remount overlay read-only to prevent UBIFS background writes
        run_ssh_cmd(transport, "sync; mount -o remount,ro /overlay 2>/dev/null || true")

        # Step 1: Upload and flash kernel (4 MB -> kernel partition)
        log_fn("Adım 1/3: Kernel + RootFS başlangıcı (4 MB) cihaza gönderiliyor...")
        upload_bytes_via_ssh(
            transport,
            "/tmp/stock_kernel.bin",
            kernel_4m,
            log_fn,
            lambda s, t: set_progress(12 + int(10 * s / t)),
        )
        log_fn("Adım 1/3: 'kernel' (mtd7) bölümü NAND flash'a yazılıyor...")
        rc, out, err = run_ssh_cmd(transport, "mtd write /tmp/stock_kernel.bin kernel && rm -f /tmp/stock_kernel.bin", timeout=180)
        if rc != 0:
            raise RuntimeError(f"Kernel yazma hatası (kod {rc}): {out} {err}")
        set_progress(30)
        log_fn("Adım 1/3 tamamlandı: Kernel bölümü yazıldı.")

        # Step 2: Upload and flash rootfs_25m (25 MB -> ubiconcat1 partition)
        log_fn("Adım 2/3: RootFS ana bölümü (25 MB) cihaza gönderiliyor...")
        upload_bytes_via_ssh(
            transport,
            "/tmp/stock_rootfs.bin",
            rootfs_25m,
            log_fn,
            lambda s, t: set_progress(30 + int(30 * s / t)),
        )
        log_fn("Adım 2/3: 'ubiconcat1' (mtd8) bölümü siliniyor ve 25 MB RootFS yazılıyor (bu işlem ~45 sn sürer)...")
        rc, out, err = run_ssh_cmd(
            transport,
            "mtd erase ubiconcat1 && mtd write /tmp/stock_rootfs.bin ubiconcat1 && rm -f /tmp/stock_rootfs.bin",
            timeout=300,
        )
        if rc != 0:
            raise RuntimeError(f"ubiconcat1 yazma hatası (kod {rc}): {out} {err}")
        set_progress(75)
        log_fn("Adım 2/3 tamamlandı: RootFS (ubiconcat1) bölümü yazıldı.")

        # Step 3: Stage binaries into /tmp/ram_bin (tmpfs in RAM) and write ubiconcat0 (8 MB web) + reboot!
        log_fn("Adım 3/3: Web / Config bölümü (8 MB) cihaza gönderiliyor...")
        upload_bytes_via_ssh(
            transport,
            "/tmp/stock_web.bin",
            web_8m,
            log_fn,
            lambda s, t: set_progress(75 + int(12 * s / t)),
        )

        log_fn("Adım 3/3: RAM üzerinden 'ubiconcat0' (Web - 8 MB) yazılıp cihaz yeniden başlatılıyor...")
        erase_prefix = "/tmp/ram_bin/libc.so --library-path /tmp/ram_bin /tmp/ram_bin/mtd erase ubiconcat0 && " if web_seek_offset > 0 else ""
        stage_and_flash_cmd = (
            "mkdir -p /tmp/ram_bin && "
            "cp -f /bin/busybox /sbin/mtd /tmp/ram_bin/ && "
            "cp -f /lib/libc.so /tmp/ram_bin/libc.so && "
            "cp -f /lib/libgcc_s.so* /tmp/ram_bin/ 2>/dev/null || true && "
            "chmod +x /tmp/ram_bin/* && "
            f"{erase_prefix}"
            f"/tmp/ram_bin/libc.so --library-path /tmp/ram_bin /tmp/ram_bin/mtd -r -p {hex(web_seek_offset)} write /tmp/stock_web.bin ubiconcat0"
        )
        try:
            rc, out, err = run_ssh_cmd(transport, stage_and_flash_cmd, timeout=180)
            # Note: mtd -r triggers kernel reboot immediately upon finishing write, which may close SSH with rc=-1
            if rc not in (0, -1):
                raise RuntimeError(f"ubiconcat0 (Web) yazma hatası (kod {rc}): {out} {err}")
        except ( EOFError, OSError, paramiko.SSHException ):
            # Expected when mtd -r calls SYS_reboot and drops the TCP connection immediately
            pass

        set_progress(100)
        log_fn("İŞLEM BAŞARIYLA TAMAMLANDI! Cihaz şu anda yeniden başlatılıyor ve seçtiğiniz Stok ZTE yazılımıyla (192.168.0.1) açılacak.")
    finally:
        transport.close()


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("ZTE MF286R - OpenWrt -> Stok Yazılıma Dönüş Aracı")
        self.root.geometry("680x480")
        self.root.resizable(False, False)

        main = ttk.Frame(root, padding=14)
        main.pack(fill=tk.BOTH, expand=True)

        title_lbl = ttk.Label(
            main,
            text="ZTE MF286R OpenWrt -> Stok Yazılıma Dönüş Aracı",
            font=("Segoe UI", 12, "bold"),
        )
        title_lbl.pack(anchor=tk.W, pady=(0, 10))

        form = ttk.Frame(main)
        form.pack(fill=tk.X, pady=4)

        ttk.Label(form, text="Router IP (OpenWrt):").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        self.ip_var = tk.StringVar(value="192.168.1.1")
        ttk.Entry(form, textvariable=self.ip_var, width=18).grid(row=0, column=1, sticky=tk.W, pady=4)

        ttk.Label(form, text="SSH Kullanıcı:").grid(row=0, column=2, sticky=tk.W, padx=(16, 8), pady=4)
        self.user_var = tk.StringVar(value="root")
        ttk.Entry(form, textvariable=self.user_var, width=12).grid(row=0, column=3, sticky=tk.W, pady=4)

        ttk.Label(form, text="SSH Şifre (boş=yok):").grid(row=0, column=4, sticky=tk.W, padx=(16, 8), pady=4)
        self.pass_var = tk.StringVar(value="")
        ttk.Entry(form, textvariable=self.pass_var, show="*", width=14).grid(row=0, column=5, sticky=tk.W, pady=4)

        file_frame = ttk.Frame(main)
        file_frame.pack(fill=tk.X, pady=8)
        ttk.Label(file_frame, text="Yüklenecek Stok Yazılım (.exe veya root_uImage):").pack(anchor=tk.W)

        sel_row = ttk.Frame(file_frame)
        sel_row.pack(fill=tk.X, pady=4)
        self.file_var = tk.StringVar()
        self.combo = ttk.Combobox(sel_row, textvariable=self.file_var, width=72)
        self.combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(sel_row, text="Dosya Seç...", command=self.browse_file).pack(side=tk.RIGHT)

        self.populate_stock_files()

        self.progress_var = tk.IntVar(value=0)
        self.progress = ttk.Progressbar(main, variable=self.progress_var, maximum=100)
        self.progress.pack(fill=tk.X, pady=8)

        self.start_btn = ttk.Button(main, text="Stok Yazılıma Dön (Başlat)", command=self.start_restore)
        self.start_btn.pack(fill=tk.X, ipady=4, pady=(0, 8))

        ttk.Label(main, text="İşlem Kayıtları:").pack(anchor=tk.W)
        self.log_box = tk.Text(main, height=13, state=tk.DISABLED, font=("Consolas", 9))
        self.log_box.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

    def populate_stock_files(self):
        base_dirs = [os.getcwd(), r"C:\ZTEMF286r"]
        if getattr(sys, "frozen", False):
            base_dirs.insert(0, os.path.dirname(sys.executable))
        found = []
        seen = set()
        for d in base_dirs:
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                low = fn.lower()
                if low.startswith("dl_mf286r_") and low.endswith(".exe") and "openwrt" not in low:
                    full = os.path.abspath(os.path.join(d, fn))
                    if full not in seen:
                        seen.add(full)
                        found.append(full)
        self.combo["values"] = found
        if found:
            self.file_var.set(found[0])

    def browse_file(self):
        p = filedialog.askopenfilename(
            title="Stok Kurulum .exe veya root_uImage Seçin",
            filetypes=[("ZTE Kurulum Paketi / Imaj", "*.exe root_uImage *.*")],
        )
        if p:
            self.file_var.set(p)

    def log(self, msg):
        def _append():
            self.log_box.configure(state=tk.NORMAL)
            self.log_box.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            self.log_box.see(tk.END)
            self.log_box.configure(state=tk.DISABLED)
        self.root.after(0, _append)

    def set_progress(self, val):
        self.root.after(0, lambda: self.progress_var.set(val))

    def start_restore(self):
        target_file = self.file_var.get().strip()
        if not target_file or not os.path.exists(target_file):
            messagebox.showerror("Hata", "Lütfen geçerli bir stok yazılım (.exe veya root_uImage) dosyası seçin!")
            return
        host = self.ip_var.get().strip() or "192.168.1.1"
        user = self.user_var.get().strip() or "root"
        pw = self.pass_var.get()

        if not messagebox.askyesno(
            "Onay",
            f"Cihaz ({host}) üzerindeki OpenWrt kaldırılıp şu stok yazılım yüklenecek:\n\n{os.path.basename(target_file)}\n\nDevam etmek istiyor musunuz?",
        ):
            return

        self.start_btn.configure(state=tk.DISABLED)
        threading.Thread(target=self._worker, args=(host, user, pw, target_file), daemon=True).start()

    def _worker(self, host, user, pw, target_file):
        try:
            restore_stock_firmware(host, user, pw, target_file, self.log, self.set_progress)
            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "Başarılı",
                    "Stok yazılım başarıyla NAND flash'a yazıldı!\nCihaz yeniden başlıyor (~60 sn içinde 192.168.0.1 arayüzü açılacaktır).",
                ),
            )
        except Exception as e:
            self.log(f"HATA: {e}")
            self.root.after(0, lambda: messagebox.showerror("Hata", str(e)))
        finally:
            self.root.after(0, lambda: self.start_btn.configure(state=tk.NORMAL))


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
