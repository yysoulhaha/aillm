# -*- coding: utf-8 -*-
"""AI-LLM → Ubuntu 安装包 .deb 生成器（纯 Python，Windows/Linux 均可运行）。

原理：.deb = ar 归档（debian-binary + control.tar.gz + data.tar.gz）。
本脚本不依赖 dpkg/ar（Windows 无），直接在内存中拼装 ar。

用法：
    python packaging/make_deb.py                     # 输出 dist/ai-llm-gateway_<版本>_all.deb
    python packaging/make_deb.py --out out.deb

说明：
- 载荷 = 运行时源码（server.py/client.py/core/ web/ requirements.txt）
        + packaging/deb_src/（DEBIAN/脚本、/usr/bin 启动器、桌面项、systemd unit）
- 版本号从 server.py 的 APP_VERSION 自动读取（单一事实来源）。
- 首次安装 postinst 创建 /opt/AI-LLM/.venv 并 pip 安装依赖（零编译、可直接装）；
  用户数据首启自动落在 ~/.local/share/ai-llm。
"""
import argparse
import io
import os
import pathlib
import re
import tarfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

_AR_TYPES = {
    ".py": 0o100644, "requirements.txt": 0o100644,
}

_CONTROL_FILES = ("control", "postinst", "prerm")
_SCRIPT_MODE = 0o100755


def app_version() -> str:
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', src)
    return m.group(1) if m else "0.0.0"


def _tarinfo(name: str, size: int, mode: int, typeflag: str = tarfile.REGTYPE):
    ti = tarfile.TarInfo(name)
    ti.size = size
    ti.mode = mode
    ti.type = typeflag
    ti.mtime = 0
    ti.uid = 0
    ti.gid = 0
    ti.uname = "root"
    ti.gname = "root"
    return ti


def payload_files():
    """返回相对『data 根』-> 源文件路径 的映射，仅收运行时需要的文件。"""
    items = {}
    for name in ("server.py", "client.py", "requirements.txt"):
        p = ROOT / name
        if p.exists():
            items[f"opt/AI-LLM/{name}"] = p
    for p in (ROOT / "core").glob("*.py"):
        items[f"opt/AI-LLM/core/{p.name}"] = p
    for p in (ROOT / "web").iterdir():
        if p.is_file():
            items[f"opt/AI-LLM/web/{p.name}"] = p
    # deb_src 静态文件（usr/ 部分）
    for p in (HERE / "deb_src" / "usr").rglob("*"):
        if p.is_file():
            rel = pathlib.PurePosixPath(*p.relative_to(HERE / "deb_src").parts)
            items[str(rel)] = p
    return items


def _mode_for(rel: str) -> int:
    if rel == "usr/bin/ai-llm":
        return 0o100755
    if rel.endswith(".py"):
        return 0o100644
    return 0o100644


def _add_tree(tar: tarfile.TarFile, prefix: str, items: dict):
    # 目录项
    dirs = {str(pathlib.PurePosixPath(p).parent) for p in items}
    for d in sorted(dirs):
        if d in (".", ""):
            continue
        ti = _tarinfo(f"{prefix}/{d}" if prefix else d, 0, 0o100755, tarfile.DIRTYPE)
        tar.addfile(ti)
    # 文件项
    for rel in sorted(items):
        data = items[rel].read_bytes()
        ti = _tarinfo(f"{prefix}/{rel}" if prefix else rel, len(data), _mode_for(rel))
        tar.addfile(ti, io.BytesIO(data))


def _ar_member(name: str, data: bytes) -> bytes:
    """单个 ar 成员（System V / GNU ar 格式）：60 字节头 + 数据(+偶数对齐)。"""
    size = len(data)
    header = (
        name[:16].ljust(16)
        + str(0).rjust(12)        # mtime
        + "0".rjust(6)            # uid
        + "0".rjust(6)            # gid
        + "100644".rjust(8)       # mode
        + str(size).rjust(10)     # size
        + "`\n"                   # 结束符
    ).encode("ascii")
    pad = b"\n" if size % 2 else b""
    return header + data + pad


def write_ar(out: pathlib.Path, members: list):
    """把 [(名字, 字节)] 写成 ar 归档（= .deb）。"""
    blob = b"!<arch>\n" + b"".join(_ar_member(n, d) for n, d in members)
    out.write_bytes(blob)


def build_deb(out: pathlib.Path, version: str):
    out.parent.mkdir(parents=True, exist_ok=True)

    # ---------- control.tar.gz ----------
    # 标准 .deb 布局：control/postinst/prerm 放归档根（同 dpkg-deb -b 的 "./member"）。
    # 若放进 DEBIAN/ 子目录，dpkg 1.20+ 会报「未发现 control 文件」而无法安装。
    buf_c = io.BytesIO()
    with tarfile.open(fileobj=buf_c, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        items = [("control", 0o100644, None)]  # name, mode, bytes 占位
        ctrl = (HERE / "deb_src" / "DEBIAN" / "control").read_text(encoding="utf-8")
        ctrl = re.sub(r"(?m)^Version:.*$", f"Version: {version}", ctrl)
        items[0] = ("control", 0o100644, ctrl.encode("utf-8") + b"\n")
        for name in ("postinst", "prerm"):
            p = HERE / "deb_src" / "DEBIAN" / name
            if p.exists():
                items.append((name, _SCRIPT_MODE, p.read_bytes()))
        for name, mode, data in items:
            ti = _tarinfo(f"./{name}", len(data), mode)
            tar.addfile(ti, io.BytesIO(data))
    control_gz = buf_c.getvalue()

    # ---------- data.tar.gz ----------
    payload = payload_files()
    buf_d = io.BytesIO()
    with tarfile.open(fileobj=buf_d, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        _add_tree(tar, "", dict(sorted(payload.items())))
    data_gz = buf_d.getvalue()

    # ---------- ar (deb) ----------
    members = [
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", control_gz),
        ("data.tar.gz", data_gz),
    ]
    write_ar(out, members)
    return out


def main():
    ap = argparse.ArgumentParser(description="AI-LLM .deb 打包")
    ap.add_argument("--out", default=None, help="输出 .deb 路径（默认 dist/）")
    ap.add_argument("--version", default=None, help=f"版本号（默认读 server.py APP_VERSION={app_version()}）")
    args = ap.parse_args()
    version = args.version or app_version()
    out = pathlib.Path(args.out) if args.out else ROOT / "dist" / f"ai-llm-gateway_{version}_all.deb"
    build_deb(out, version)
    print(f"[deb] OK {out} ({out.stat().st_size / 1024 / 1024:.1f} MB), version={version}")


if __name__ == "__main__":
    main()