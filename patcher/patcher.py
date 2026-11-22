"""Standalone Windows patcher for Dino Crisis Korean v0.81."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading

VERSION = '0.81'
RESOURCES = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_manifest(payload):
    data = json.loads((Path(payload) / 'manifest.json').read_text('utf-8'))
    if data.get('version') != 'v' + VERSION:
        raise ValueError('이 패처와 맞지 않는 패치 데이터입니다.')
    return data


def member(payload, name):
    base = Path(payload).resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base) or path == base:
        raise ValueError('잘못된 패치 데이터 경로입니다.')
    return path


def read_gdi(path):
    lines = path.read_text('utf-8-sig').splitlines()
    rows = []
    for line in lines[1:]:
        values = shlex.split(line)
        if not values:
            continue
        rows.append(dict(number=int(values[0]), lba=int(values[1]), type=int(values[2]),
                         sector=int(values[3]), file=values[4], offset=int(values[5])))
    if int(lines[0]) != 3 or [r['number'] for r in rows] != [1, 2, 3]:
        raise ValueError('추출한 디스크의 트랙 구성이 다릅니다.')
    return rows


def cook_track(source, dest, sector):
    if sector != 2352 or source.stat().st_size % 2352:
        raise ValueError('지원하지 않는 데이터 트랙 형식입니다.')
    with source.open('rb') as reader, dest.open('xb') as writer:
        while chunk := reader.read(2352 * 4096):
            writer.write(b''.join(chunk[p + 16:p + 2064] for p in range(0, len(chunk), 2352)))


def checked_file(path, expected, message):
    if not path.is_file() or path.stat().st_size != expected['bytes'] or sha256(path) != expected['sha256']:
        raise ValueError(message)
    return path


def apply(source, output, *, payload=None, log=lambda value: None):
    payload = Path(payload or RESOURCES / 'payload').resolve()
    manifest = read_manifest(payload)
    tools = json.loads((payload / 'tool-hashes.json').read_text('utf-8'))
    source = Path(source).resolve()
    requested_output = Path(output)
    output = requested_output.parent.resolve() / requested_output.name
    if output.exists() or output.is_symlink():
        raise ValueError('출력 파일이 이미 있습니다. 새 파일 이름을 지정하세요.')
    if not output.parent.is_dir():
        raise ValueError('출력 폴더를 찾을 수 없습니다.')

    log('원본 일본판 CHD 확인 중…')
    checked_file(source, manifest['source'], '원본 CHD의 크기 또는 SHA-256이 다릅니다. 일본판 원본을 선택하세요.')
    payload_error = '패치 데이터가 손상되었습니다. EXE를 다시 받아 주세요.'
    chdman = checked_file(member(payload, tools['chdman']['name']), tools['chdman'], payload_error)
    xdelta = checked_file(member(payload, tools['xdelta']['name']), tools['xdelta'], payload_error)
    delta = checked_file(member(payload, manifest['patch']['name']), manifest['patch'], payload_error)
    if shutil.disk_usage(output.parent).free < 3_000_000_000:
        raise ValueError('출력 드라이브에 3GB 이상의 여유 공간이 필요합니다.')

    with tempfile.TemporaryDirectory(prefix='.dino-v08-', dir=output.parent) as folder:
        workspace = Path(folder)

        def run(*command):
            process = subprocess.run([str(v) for v in command], capture_output=True,
                                     cwd=workspace, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if process.returncode:
                detail = (process.stdout + process.stderr).decode('utf-8', 'replace')[-1000:]
                raise RuntimeError('패치 도구 실행에 실패했습니다.\n' + detail)

        log('원본 CHD에서 디스크 트랙 추출 중…')
        gdi = workspace / 'source.gdi'
        run(chdman, 'extractcd', '-i', source, '-o', gdi)
        tracks = read_gdi(gdi)
        track1, track2, track3 = tracks
        if [(t['lba'], t['type'], t['offset']) for t in tracks] != [(0, 4, 0), (600, 0, 0), (45000, 4, 0)]:
            raise ValueError('추출한 디스크의 트랙 배치가 다릅니다.')
        if [track1['sector'], track2['sector']] != [2352, 2352]:
            raise ValueError('추출한 부팅 트랙의 섹터 크기가 다릅니다.')
        source_track = member(workspace, track3['file'])
        if track3['sector'] == 2352:
            cooked = workspace / 'source03.iso'
            cook_track(source_track, cooked, track3['sector'])
            source_track.unlink()
            source_track = cooked
        elif track3['sector'] != 2048:
            raise ValueError('지원하지 않는 데이터 트랙 형식입니다.')
        checked_file(source_track, manifest['track03_source'], '원본 데이터 트랙의 SHA-256이 다릅니다.')

        log('데이터 트랙에 한국어 패치 적용 중…')
        patched = workspace / 'track03.iso'
        run(xdelta, '-d', '-s', source_track, delta, patched)
        checked_file(patched, manifest['track03_target'], '패치한 트랙의 검증에 실패했습니다.')
        source_track.unlink()

        log('한국어 CHD 생성 중…')
        patched_gdi = workspace / 'korean.gdi'
        # Relative ASCII names support CHD creation inside a Unicode directory.
        names = [t['file'] for t in tracks[:2]]
        if names != ['source01.bin', 'source02.raw']:
            raise ValueError('추출한 트랙 파일 이름이 다릅니다.')
        patched_gdi.write_text('3\n1 0 4 2352 source01.bin 0\n2 600 0 2352 source02.raw 0\n'
                               '3 45000 4 2048 track03.iso 0\n', encoding='ascii')
        temporary = workspace / 'result.chd'
        run(chdman, 'createcd', '-i', patched_gdi.name, '-o', temporary.name)
        log('완성 CHD의 무결성과 SHA-256 확인 중…')
        run(chdman, 'verify', '-i', temporary)
        checked_file(temporary, manifest['target'], '완성 CHD의 SHA-256이 다릅니다.')
        # On Windows os.rename fails if the destination already exists.
        if os.name == 'nt':
            temporary.rename(output)
        else:
            os.link(temporary, output)
        log('생성 완료 · ' + str(output))
        return {'success': True, 'version': VERSION, 'output': str(output),
                'source_sha256': manifest['source']['sha256'],
                'output_size': output.stat().st_size, 'output_sha256': manifest['target']['sha256'],
                'chd_internal_integrity_verified': True}


def create_gui(payload=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    payload = Path(payload or RESOURCES / 'payload')
    manifest = read_manifest(payload)
    root = tk.Tk()
    root.title('디노 크라이시스 한국어 패치 v0.81')
    root.geometry('760x480')
    root.minsize(700, 480)
    frame = ttk.Frame(root, padding=22)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='디노 크라이시스', font=('맑은 고딕', 20, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='Dreamcast 일본판 한국어 패치 v0.81',
              font=('맑은 고딕', 11)).pack(anchor='w', pady=(3, 16))
    source = tk.StringVar()
    output = tk.StringVar()
    status = tk.StringVar(value='원본 일본판 CHD를 선택하세요. 출력 드라이브 여유 공간 3GB 이상이 필요합니다.')
    source_entry = output_entry = None

    def select_source():
        chosen = filedialog.askopenfilename(parent=root, title='원본 일본판 CHD 선택',
                                            filetypes=[('CHD 이미지', '*.chd'), ('모든 파일', '*.*')])
        if chosen:
            source.set(chosen)
            if not output.get():
                output.set(str(Path(chosen).parent / manifest['target']['name']))

    def select_output():
        chosen = filedialog.asksaveasfilename(parent=root, title='새 한국어 CHD 저장 위치',
                                            initialfile=manifest['target']['name'], defaultextension='.chd',
                                            filetypes=[('CHD 이미지', '*.chd')])
        if chosen:
            output.set(chosen)

    controls = []
    for label, variable, command in [('원본 CHD', source, select_source), ('출력 CHD', output, select_output)]:
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=7)
        ttk.Label(row, text=label, width=10).pack(side='left')
        entry = ttk.Entry(row, textvariable=variable)
        entry.pack(side='left', fill='x', expand=True)
        button = ttk.Button(row, text='선택', command=command)
        button.pack(side='left', padx=(8, 0))
        controls.extend([entry, button])
        if variable is source:
            source_entry = entry
        else:
            output_entry = entry
    ttk.Label(frame, text='한글화 · 기타 깎는 노인',
              wraplength=690).pack(anchor='w', pady=(14, 9))
    ttk.Label(frame, textvariable=status, wraplength=690).pack(anchor='w', pady=5)
    progress = ttk.Progressbar(frame, mode='indeterminate')
    progress.pack(fill='x', pady=(6, 8))
    log_box = tk.Text(frame, height=4, wrap='word', state='disabled', font=('맑은 고딕', 9))
    log_box.pack(fill='both', expand=True, pady=(2, 8))
    events = queue.Queue()
    busy = False

    def start():
        nonlocal busy
        if not source.get() or not output.get():
            messagebox.showerror('파일 선택', '원본 CHD와 새 출력 CHD를 선택하세요.', parent=root)
            return
        chosen_source, chosen_output = source.get(), output.get()
        busy = True
        for widget in controls:
            widget.configure(state='disabled')
        apply_button.configure(state='disabled')
        progress.start()

        def worker():
            try:
                result = apply(chosen_source, chosen_output, payload=payload,
                               log=lambda value: events.put(('log', value)))
                events.put(('done', result))
            except Exception as error:
                events.put(('error', str(error)))
        threading.Thread(target=worker, daemon=True).start()

    apply_button = ttk.Button(frame, text='한국어 패치 적용', command=start)
    apply_button.pack(anchor='e')

    def pump():
        nonlocal busy
        try:
            while True:
                kind, value = events.get_nowait()
                line = value if kind != 'done' else '결과 SHA-256: ' + value['output_sha256']
                status.set(line)
                log_box.configure(state='normal')
                log_box.insert('end', line + '\n')
                log_box.see('end')
                log_box.configure(state='disabled')
                if kind in ['done', 'error']:
                    busy = False
                    progress.stop()
                    for widget in controls:
                        widget.configure(state='normal')
                    apply_button.configure(state='normal')
                    if kind == 'done':
                        messagebox.showinfo('패치 완료', 'CHD 생성과 해시 검증을 완료했습니다.\n' + value['output'], parent=root)
                    else:
                        messagebox.showerror('패치 중단', value, parent=root)
        except queue.Empty:
            pass
        root.after(100, pump)

    def close():
        if busy:
            messagebox.showinfo('패치 적용 중', '파일 생성이 끝난 뒤 닫아 주세요.', parent=root)
        else:
            root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    root.after(100, pump)
    return root, dict(source=source, output=output, source_entry=source_entry,
                      output_entry=output_entry, apply_button=apply_button)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--payload', type=Path, help='개발·검증용 데이터 경로')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--self-test-ui', action='store_true')
    parser.add_argument('--version', action='version', version='V' + VERSION)
    args = parser.parse_args()
    result = None
    code = 0
    if args.self_test_ui:
        root, widgets = create_gui(args.payload)
        root.withdraw()
        root.update_idletasks()
        assert widgets['source_entry'].winfo_exists() and widgets['output_entry'].winfo_exists()
        assert widgets['apply_button'].cget('text') == '한국어 패치 적용'
        assert root.title().endswith('v0.81')
        result = dict(success=True, version=VERSION, source_and_output_controls=True,
                      patch_button=True, korean_title=True, visual_screenshot_verified=False)
        root.destroy()
    elif args.source or args.output:
        if not (args.source and args.output):
            parser.error('--source와 --output을 함께 지정하세요.')
        try:
            result = apply(args.source, args.output, payload=args.payload)
        except Exception as error:
            result = dict(success=False, version=VERSION, error=str(error))
            code = 1
    else:
        root, _ = create_gui(args.payload)
        root.mainloop()
    if result is not None:
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
        if sys.stdout:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
