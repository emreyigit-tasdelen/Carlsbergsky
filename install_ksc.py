#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Emre Yiğit Taşdelen <https://github.com/emreyigit-tasdelen>
"""Carlsbergsky: remote first-time installer for Kaspersky Security Center Linux.

Drives Kaspersky's official "quick installation script" (ksc_fast_install.py)
on an empty Linux server over SSH:

  1. Finds the packages in a release folder (deb/rpm, chosen by the server OS)
  2. Connects over SSH and checks OS support and resources against the
     official tool's own tables
  3. Verifies the FQDN resolves to loopback on the server (adds /etc/hosts if needed)
  4. Refreshes package repositories (apt update / dnf makecache)
  5. Copies the files to ~/kscdistr and writes params.txt
  6. Runs sudo ./ksc_fast_install.py and prints the Web Console sign-in details

First-time installation only (the official tool does not upgrade).

Success: ~/kscdistr stays on the server.
Failure: the official tool rolls back its changes, ~/kscdistr is removed.
Interrupt (Ctrl+C / lost connection): files stay; running the script again
lets the official tool resume from the last completed step.

Example release folder:
    16.3/
      ksc64_16.3.0-1207_amd64.deb           (or .rpm)
      ksc-web-console-16.3.13218.x86_64.deb (or .rpm)
      ksc_fast_install.tar.gz

Requires:  pip install -r requirements.txt
Usage:     python install_ksc.py
"""
import ast
import getpass
import io
import os
import posixpath
import re
import shlex
import sys
import tarfile

try:
    import paramiko
except ImportError:
    sys.exit("paramiko is missing. Install it: pip install -r requirements.txt")

__version__ = "1.0.0"

WORK_DIR_NAME = "kscdistr"                        # example folder from the KSC guide
FAST_INSTALL = "ksc_fast_install.py"
FLAG_DIR = "/var/opt/kaspersky/ksc_fast_install"  # state/log folder of the official tool
DEB_LIKE = {"debian", "ubuntu", "astra"}


# ------------------------------------------------------------------ local side

def find_files(folder):
    """Finds the KSC / Web Console packages and the installer bundle (subfolders included)."""
    found = {"ksc": {}, "nwc": {}, "fast": None}
    for root, _, files in os.walk(folder):
        for f in files:
            p = os.path.join(root, f)
            low = f.lower()
            ext = "deb" if low.endswith(".deb") else "rpm" if low.endswith(".rpm") else None
            if ext and low.startswith("ksc64"):
                found["ksc"].setdefault(ext, p)
            elif ext and low.startswith("ksc-web-console"):
                found["nwc"].setdefault(ext, p)
            elif low.startswith("ksc_fast_install") and low.endswith((".tar.gz", ".py")):
                found["fast"] = found["fast"] or p
    return found


def read_fast_bundle(path):
    """Returns the files of the installer bundle as {name: bytes}."""
    out = {}
    if path.endswith(".py"):
        folder = os.path.dirname(path)
        for f in os.listdir(folder):
            fp = os.path.join(folder, f)
            if os.path.isfile(fp) and f.endswith((".py", ".txt", ".sha256")):
                with open(fp, "rb") as fh:
                    out[f] = fh.read()
    else:
        with tarfile.open(path, "r:gz") as tar:
            for m in tar.getmembers():
                name = posixpath.basename(m.name)
                if m.isfile() and name and not name.startswith("."):
                    out[name] = tar.extractfile(m).read()
    if FAST_INSTALL not in out:
        sys.exit(f"ERROR: {FAST_INSTALL} not found in {path}.")
    return out


def literal_table(script_src, name):
    """Reads a constant dict from the official script (handles NETSIZE.X and a + b)."""
    m = re.search(rf"^{name}\s*=\s*(\{{.*?^\}})", script_src, re.S | re.M)
    if not m:
        return None
    src = re.sub(r"NETSIZE\.(\w+)", lambda x: repr(x.group(1).lower()), m.group(1))
    src = re.sub(r"(\d+)\s*\+\s*(\d+)", lambda x: str(int(x.group(1)) + int(x.group(2))), src)
    try:
        return ast.literal_eval(src)
    except (ValueError, SyntaxError):
        return None


def ksc_version(pkg_path):
    m = re.search(r"ksc64[_-](\d+)\.(\d+)", os.path.basename(pkg_path))
    return f"{m.group(1)}.{m.group(2)}" if m else None


# ----------------------------------------------------------------- remote side

class Remote:
    def __init__(self, host, user, password, port):
        self.password = password
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(host, port=port, username=user, password=password, timeout=20)
        self.sftp = self.ssh.open_sftp()
        self.is_root = user == "root"

    def run(self, cmd, sudo=True, show=None, capture=False):
        """Runs cmd via bash -c, streaming output live. Returns (rc, output)."""
        full = f"bash -c {shlex.quote(cmd)}"
        if sudo and not self.is_root:
            full = f"sudo -S -p '' {full}"
        if not capture:
            print(f"\n$ {show or cmd}")
        chan = self.ssh.get_transport().open_session()
        chan.set_combine_stderr(True)
        chan.get_pty(width=200)
        chan.exec_command(full)
        # The PTY echoes the password sent to sudo: swallow the first line, never print it
        hide_echo = sudo and not self.is_root
        if hide_echo:
            chan.send(self.password + "\n")
        pre, buf = "", []
        while True:
            data = chan.recv(4096)
            if not data:
                break
            text = data.decode(errors="replace")
            if hide_echo:
                pre += text
                if "\n" not in pre:
                    continue
                text = pre.split("\n", 1)[1]
                hide_echo = False
            text = text.replace(self.password, "******")
            buf.append(text)
            if not capture:
                sys.stdout.write(text)
                sys.stdout.flush()
        return chan.recv_exit_status(), "".join(buf)

    def out(self, cmd, sudo=False):
        return self.run(cmd, sudo=sudo, capture=True)[1].strip()

    def must(self, cmd, **kw):
        rc, out = self.run(cmd, **kw)
        if rc != 0:
            sys.exit(f"\nERROR: command failed (exit {rc}): {kw.get('show') or cmd}")
        return out

    def upload(self, local, dest):
        name = os.path.basename(local)
        last = [-1]

        def cb(done, total):
            pct = int(done * 100 / total) if total else 100
            if pct != last[0] and pct % 5 == 0:
                last[0] = pct
                print(f"\r  {name}: {pct}%", end="", flush=True)

        print(f"Uploading: {name} ({os.path.getsize(local) / 1e6:.1f} MB)")
        self.sftp.put(local, dest, callback=cb)
        print()

    def write_bytes(self, data, dest):
        self.sftp.putfo(io.BytesIO(data), dest)

    def os_release(self):
        info = {}
        for line in self.out("cat /etc/os-release").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip().strip('"')
        return info

    def installed(self, pkg, fmt):
        q = (f"dpkg-query -W -f='${{Status}}' {pkg} 2>/dev/null" if fmt == "deb"
             else f"rpm -q {pkg} >/dev/null 2>&1 && echo 'install ok installed'")
        return "install ok installed" in self.out(q)

    def close(self):
        self.sftp.close()
        self.ssh.close()


# ------------------------------------------------------------------------ flow

def ask(prompt, default=None):
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def yes(prompt):
    return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes", "e", "evet")


def check_resources(r, script_src, netsize):
    """Pre-check with the official tool's thresholds. Offers a skip if short; True = skip."""
    cpu_t = literal_table(script_src, "NETSIZE_CPU_REQUIRED")
    ram_t = literal_table(script_src, "NETSIZE_RAM_REQUIRED")
    disk_t = literal_table(script_src, "NETSIZE_DISK_REQUIRED")
    if not (cpu_t and ram_t and disk_t):
        print("Note: resource tables could not be read; leaving the check to the official tool.")
        return False
    vals = r.out("nproc; awk '/MemTotal/{print $2}' /proc/meminfo; df -Pk /var | awk 'NR==2{print $4}'").split()
    try:
        cpu, mem_kb, disk_kb = (int(x) for x in vals[:3])
    except ValueError:
        return False
    ram_gb, disk_gb = mem_kb / 1024 ** 2, disk_kb / 1024 ** 2

    def need(ns):
        d = disk_t[ns]
        return cpu_t[ns]["all"], ram_t[ns]["all"], d["dbms"] + d["ksc"] + d["nwc"]

    print(f"\nServer resources: {cpu} CPU, {ram_gb:.1f} GB RAM, {disk_gb:.1f} GB free on /var")
    ns = netsize.lower()
    c, m, d = need(ns)
    if cpu >= c and ram_gb >= m and disk_gb >= d:
        print(f"'{netsize}' requirements met ({c} CPU, {m} GB RAM, {d} GB disk).")
        return False
    print(f"INSUFFICIENT: '{netsize}' needs at least {c} CPU, {m} GB RAM, {d} GB free on /var.")
    for k in cpu_t:
        c2, m2, d2 = need(k)
        print(f"  {k.upper():>2}: {c2} CPU, {m2} GB RAM, {d2} GB disk")
    print("Note: if RAM is under-reported, disable dynamic memory (ballooning) during the install.")
    print("The resource check can be skipped (NOT supported by Kaspersky; test/lab only).")
    if not yes("Skip the resource check and continue?"):
        sys.exit("Increase server resources or choose a smaller network size.")
    return True


def ensure_fqdn_loopback(r, fqdn):
    """Per the KSC guide the FQDN must resolve to loopback on the server; adds /etc/hosts if not."""
    ip = r.out(f"getent hosts {shlex.quote(fqdn)} | awk '{{print $1; exit}}'")
    if ip.startswith("127."):
        print(f"FQDN check: {fqdn} -> {ip} (loopback, OK)")
        return
    print(f"FQDN check: {fqdn} -> {ip or 'unresolved'} (must be loopback)")
    short = fqdn.split(".")[0]
    line = f"127.0.1.1 {fqdn}" + (f" {short}" if short != fqdn else "")
    if not yes(f"Add '{line}' to /etc/hosts?"):
        sys.exit("The FQDN must resolve to loopback before installing (KSC guide requirement).")
    r.must(f"cp -a /etc/hosts /etc/hosts.bak-ksc && echo {shlex.quote(line)} >> /etc/hosts",
           show=f"echo '{line}' >> /etc/hosts  (backup: /etc/hosts.bak-ksc)")


def run_installer(r, work):
    """Runs the official tool. Returns (success, output); on failure the tool has rolled back."""
    rc, out = r.run(f"cd {shlex.quote(work)} && chmod +x ./{FAST_INSTALL} && ./{FAST_INSTALL}",
                    show=f"sudo ./{FAST_INSTALL}")
    return rc == 0, out


def connect(host, user, password, port):
    try:
        return Remote(host, user, password, port)
    except paramiko.AuthenticationException:
        sys.exit("ERROR: SSH authentication failed (check user name and password).")
    except (paramiko.SSHException, OSError) as err:
        sys.exit(f"ERROR: cannot connect to {host}:{port} ({err}).")


def main():
    print(f"=== Carlsbergsky {__version__}: Kaspersky Security Center Linux remote install ===\n")
    folder = ask(r"Release folder (e.g. C:\Kaspersky\16.3)").strip('"').strip("'")
    if not os.path.isdir(folder):
        sys.exit("Folder not found.")
    files = find_files(folder)
    if not files["fast"]:
        sys.exit("ksc_fast_install.tar.gz (KSC installation script) not found.")
    if not files["ksc"]:
        sys.exit("ksc64 package (.deb/.rpm) not found.")
    bundle = read_fast_bundle(files["fast"])
    script_src = bundle[FAST_INSTALL].decode(errors="replace")

    host = ask("Server IP")
    port = int(ask("SSH port", "22"))
    user = ask("SSH user", "root")
    password = getpass.getpass("SSH password: ")

    print(f"\nConnecting to {host}...")
    r = connect(host, user, password, port)
    work = posixpath.join(r.out("echo $HOME") or f"/home/{user}", WORK_DIR_NAME)
    cleanup_on_error = False
    try:
        # 1) OS and package format
        osr = r.os_release()
        os_id, os_ver = osr.get("ID", "").lower(), osr.get("VERSION_ID", "")
        like = set(osr.get("ID_LIKE", "").lower().split())
        print(f"Server: {osr.get('PRETTY_NAME', os_id + ' ' + os_ver)}")
        fmt = "deb" if (os_id in DEB_LIKE or like & DEB_LIKE) else "rpm"
        ksc_pkg, nwc_pkg = files["ksc"].get(fmt), files["nwc"].get(fmt)
        if not ksc_pkg or not nwc_pkg:
            sys.exit(f"ERROR: the server needs .{fmt} packages; the folder must contain ksc64 and ksc-web-console .{fmt}.")

        # 2) Interrupted install? (after Ctrl+C / lost connection)
        resumable = r.out(f"test -f {shlex.quote(work)}/{FAST_INSTALL} && test -d {FLAG_DIR} && echo y", sudo=True) == "y"
        if resumable and yes(f"An interrupted installation was found ({work}). Resume it?"):
            ok, out = run_installer(r, work)
            finish(ok, out, work, host, None)
            return

        if r.installed("ksc64", fmt):
            sys.exit("\nKSC is already installed on this server. This script is for first-time installation only.")

        # 3) OS support (official tool's table)
        ver = ksc_version(ksc_pkg)
        compat = literal_table(script_src, "COMPAT_KSC_OS")
        supported = compat.get(f"ksc64-{ver}") if (compat and ver) else None
        if supported is not None:
            short = os_ver.split(".")[0]
            print(f"KSC {ver} supported OS: {', '.join(supported)}")
            if not any(s in (f"{os_id}-{os_ver}", f"{os_id}-{short}") for s in supported):
                print(f"WARNING: {os_id}-{os_ver} is not listed; the official tool will refuse to install.")
                if not yes("Continue anyway?"):
                    return

        # 4) Parameters
        print("\nInstallation requires accepting the Administration Server EULA, the Web Console EULA")
        print("and the Privacy Policy (server_license.txt / nwc_license.txt in the bundle).")
        if not yes("Have you read and do you accept these agreements?"):
            sys.exit("Agreements not accepted.")
        default_fqdn = r.out("hostname -f 2>/dev/null || hostname")
        fqdn = ask("Administration Server FQDN (e.g. ksclinux.example.com)", default_fqdn)
        print("Network size: S=1-1000, M=1001-10000, L=10001-30000, XL=30001-50000 devices")
        netsize = ask("Network size S/M/L/XL", "S").upper()
        nwc_port = ask("Web Console port", "8080")

        skip_resources = check_resources(r, script_src, netsize)
        ensure_fqdn_loopback(r, fqdn)

        params = [
            "klfi_server_eula_accepted=Y",
            "klfi_nwc_eula_accepted=Y",
            "klfi_pp_accepted=Y",
            f"klfi_klserver_address={fqdn}",
            f"klfi_kliam_address={fqdn}",
            f"klfi_display_name={fqdn}",
            f"klfi_netsize={netsize}",
            f"klfi_nwc_port={nwc_port}",
        ]

        # 5) Refresh package repositories (guide prerequisite; the DBMS comes from them)
        r.must("if command -v apt-get >/dev/null; then apt-get update; else dnf makecache; fi; "
               "command -v python3 >/dev/null || { apt-get install -y python3 2>/dev/null || dnf install -y python3; }")

        # 6) Prepare ~/kscdistr and copy files
        r.must(f"rm -rf {shlex.quote(work)}", sudo=True)
        r.must(f"mkdir -p {shlex.quote(work)}", sudo=False)
        cleanup_on_error = True
        for name, data in bundle.items():
            if name == "params.txt":
                continue
            if name == FAST_INSTALL and skip_resources:
                call = b"check_available_resources(params, is_ksc_installed, is_nwc_installed)"
                if call not in data:
                    sys.exit("ERROR: the resource check cannot be skipped in this release.")
                data = data.replace(call, b"print_warning('Resource check skipped (unsupported)')")
            r.write_bytes(data, f"{work}/{name}")
        r.write_bytes(("\n".join(params) + "\n").encode(), f"{work}/params.txt")
        r.upload(ksc_pkg, f"{work}/{os.path.basename(ksc_pkg)}")
        r.upload(nwc_pkg, f"{work}/{os.path.basename(nwc_pkg)}")
        r.must(f"chmod 600 {shlex.quote(work)}/params.txt", sudo=False)

        # 7) Install
        ok, out = run_installer(r, work)
        cleanup_on_error = not ok
        finish(ok, out, work, fqdn, nwc_port)
    except KeyboardInterrupt:
        cleanup_on_error = False
        print(f"\nInterrupted. Files were left on the server ({work}).")
        print("Run the script again to resume from the last completed step.")
        print(f"To abandon it, run on the server: cd {work} && sudo ./{FAST_INSTALL} rollback")
    finally:
        if cleanup_on_error:
            print(f"\nRemoving installation files from the server ({work}) ...")
            try:
                r.run(f"rm -rf {shlex.quote(work)}", capture=True)
            except Exception as err:
                print(f"WARNING: could not remove them, do it manually: sudo rm -rf {work} ({err})")
        r.close()


def finish(ok, out, work, address, nwc_port):
    if not ok:
        print("\nInstallation failed. The official tool rolled back its changes.")
        print(f"Logs: {FLAG_DIR}/  (sudo ls -la {FLAG_DIR})")
        sys.exit(1)
    m = re.search(r"User:\s*(\S+).*?Pass:\s*(\S+)", out, re.S)
    print("\n=== Installation complete ===")
    print(f"Web Console: https://{address}:{nwc_port or '8080'}")
    if m:
        print(f"User: {m.group(1)}   Password: {m.group(2)}   <- SAVE IT, it is not shown again")
    print(f"Installation files on the server: {work}")
    print(f"\nIMPORTANT: sign-in redirects to {address} (IAM, port 4444). On the computer that opens")
    print("the Web Console this name must resolve to the server IP: add a DNS A record or a hosts entry.")


if __name__ == "__main__":
    main()
