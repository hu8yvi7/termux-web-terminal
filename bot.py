#!/usr/bin/env python3
"""
Termux Web Terminal Pro - Complete Web-based Terminal
- All Termux commands support
- Real-time output streaming
- File upload/download
- Process management with /stop
- Live logs display
- File manager
"""
import os
import sys
import subprocess
import threading
import time
import json
import signal
import psutil
import shutil
from datetime import datetime
from pathlib import Path
from werkzeug.utils import secure_filename
from flask import Flask, request, jsonify, render_template_string, Response, send_file

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max file size

# Global state
class TerminalState:
    def __init__(self):
        self.current_dir = os.getcwd()
        self.processes = {}  # pid -> {'process': obj, 'cmd': str, 'type': 'python/bash'}
        self.command_history = []
        self.logs = []
        self.upload_dir = os.path.join(os.getcwd(), 'uploads')
        os.makedirs(self.upload_dir, exist_ok=True)

state = TerminalState()

# ==================== UTILITY FUNCTIONS ====================
def log_message(msg, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] [{level}] {msg}"
    state.logs.append(log_entry)
    if len(state.logs) > 1000:  # Keep last 1000 logs
        state.logs = state.logs[-1000:]
    print(log_entry)
    return log_entry

def kill_process_tree(pid):
    """Kill process and all children"""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        
        for child in children:
            try:
                child.kill()
            except:
                pass
        
        try:
            parent.kill()
        except:
            pass
        
        return True
    except psutil.NoSuchProcess:
        return False
    except Exception as e:
        log_message(f"Error killing process {pid}: {e}", "ERROR")
        return False

def get_all_processes():
    """Get all processes started from this terminal"""
    procs = []
    for pid, info in state.processes.items():
        try:
            proc = psutil.Process(pid)
            procs.append({
                'pid': pid,
                'cmd': info['cmd'],
                'status': 'running' if proc.is_running() else 'dead',
                'start_time': datetime.fromtimestamp(proc.create_time()).strftime("%H:%M:%S"),
                'type': info.get('type', 'unknown')
            })
        except:
            procs.append({
                'pid': pid,
                'cmd': info['cmd'],
                'status': 'dead',
                'start_time': 'unknown',
                'type': info.get('type', 'unknown')
            })
    return procs

def execute_command_stream(cmd, cwd):
    """Execute command and stream output"""
    log_message(f"Executing: {cmd}", "COMMAND")
    
    try:
        process = subprocess.Popen(
            cmd,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        pid = process.pid
        state.processes[pid] = {
            'process': process,
            'cmd': cmd,
            'type': 'python' if 'python' in cmd.lower() else 'bash'
        }
        
        # Read output line by line
        for line in iter(process.stdout.readline, ''):
            if line:
                yield line.rstrip() + "\n"
        
        process.wait()
        
        # Remove from active processes
        if pid in state.processes:
            del state.processes[pid]
            
        yield f"\n[Process completed with exit code: {process.returncode}]\n"
        
    except Exception as e:
        yield f"Error executing command: {str(e)}\n"

# ==================== HTML TEMPLATES ====================

# ==================== FLASK ROUTES ====================
@app.route('/')
def index():
    return INDEX_HTML

@app.route('/api/execute', methods=['POST'])
def api_execute():
    """Execute command with streaming"""
    data = request.json
    cmd = data.get('command', '').strip()
    
    if not cmd:
        return jsonify({'error': 'No command provided'})
    
    def generate():
        for line in execute_command_stream(cmd, state.current_dir):
            yield line
    
    return Response(generate(), mimetype='text/plain')

@app.route('/api/stop', methods=['POST'])
def api_stop():
    """Stop a script by filename"""
    data = request.json
    filename = data.get('filename', '')
    
    log_message(f"Stop requested for: {filename}", "PROCESS")
    
    # Find and kill process
    killed = []
    for pid, info in list(state.processes.items()):
        if filename in info['cmd']:
            if kill_process_tree(pid):
                del state.processes[pid]
                killed.append(pid)
                log_message(f"Killed process {pid} for {filename}", "PROCESS")
    
    # Also check system processes
    for proc in psutil.process_iter(['pid', 'cmdline']):
        try:
            cmdline = ' '.join(proc.info['cmdline'] or [])
            if filename in cmdline:
                if kill_process_tree(proc.info['pid']):
                    killed.append(proc.info['pid'])
                    log_message(f"Killed system process {proc.info['pid']} for {filename}", "PROCESS")
        except:
            pass
    
    if killed:
        return jsonify({
            'success': True,
            'message': f'Stopped {len(killed)} processes for {filename}',
            'killed': killed
        })
    
    return jsonify({
        'success': False,
        'message': f'No process found for {filename}'
    })

@app.route('/api/stop_pid', methods=['POST'])
def api_stop_pid():
    """Stop process by PID"""
    data = request.json
    pid = data.get('pid')
    
    try:
        if kill_process_tree(pid):
            if pid in state.processes:
                del state.processes[pid]
            return jsonify({'success': True, 'message': f'Process {pid} stopped'})
        else:
            return jsonify({'success': False, 'message': f'Process {pid} not found'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/kill_all', methods=['POST'])
def api_kill_all():
    """Kill all processes"""
    killed = []
    
    # Kill processes from state
    for pid in list(state.processes.keys()):
        if kill_process_tree(pid):
            killed.append(pid)
            del state.processes[pid]
    
    # Kill Python processes
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if 'python' in proc.info['name'].lower():
                if kill_process_tree(proc.info['pid']):
                    killed.append(proc.info['pid'])
        except:
            pass
    
    log_message(f"Killed all processes: {killed}", "SYSTEM")
    
    return jsonify({
        'success': True,
        'message': f'Killed {len(killed)} processes',
        'killed': killed
    })

@app.route('/api/ctrl', methods=['POST'])
def api_ctrl():
    """Send control signal"""
    data = request.json
    key = data.get('key', 'C')
    
    # Send to all processes
    for pid in list(state.processes.keys()):
        try:
            if key == 'C':
                os.kill(pid, signal.SIGINT)
            elif key == 'Z':
                os.kill(pid, signal.SIGTSTP)
            log_message(f"Sent Ctrl+{key} to process {pid}", "SIGNAL")
        except:
            pass
    
    return jsonify({'success': True})

@app.route('/api/files')
def api_files():
    """Get list of files in current directory"""
    files = []
    try:
        for item in os.listdir(state.current_dir):
            full_path = os.path.join(state.current_dir, item)
            if os.path.isdir(full_path):
                files.append({
                    'name': item,
                    'type': 'dir',
                    'size': 'DIR'
                })
            else:
                size = os.path.getsize(full_path)
                size_str = f"{size:,} bytes"
                if size > 1024*1024:
                    size_str = f"{size/(1024*1024):.1f} MB"
                elif size > 1024:
                    size_str = f"{size/1024:.1f} KB"
                
                files.append({
                    'name': item,
                    'type': 'file',
                    'size': size_str
                })
    except Exception as e:
        log_message(f"Error listing files: {e}", "ERROR")
    
    return jsonify({'files': files, 'current_dir': state.current_dir})

@app.route('/api/cd', methods=['POST'])
def api_cd():
    """Change directory"""
    data = request.json
    dir_path = data.get('dir', '')
    
    try:
        if dir_path == '..':
            new_dir = os.path.dirname(state.current_dir)
        elif dir_path.startswith('/'):
            new_dir = dir_path
        else:
            new_dir = os.path.join(state.current_dir, dir_path)
        
        if os.path.isdir(new_dir):
            state.current_dir = os.path.abspath(new_dir)
            log_message(f"Changed directory to: {state.current_dir}", "SYSTEM")
            return jsonify({'success': True, 'current_dir': state.current_dir})
        else:
            return jsonify({'success': False, 'message': 'Directory not found'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/read_file')
def api_read_file():
    """Read file content"""
    filename = request.args.get('file', '')
    file_path = os.path.join(state.current_dir, filename)
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return content
    except UnicodeDecodeError:
        try:
            with open(file_path, 'rb') as f:
                content = f.read().decode('latin-1')
            return content
        except:
            return "Binary file - cannot display"
    except Exception as e:
        return f"Error reading file: {str(e)}"

@app.route('/api/write_file', methods=['POST'])
def api_write_file():
    """Write file content"""
    data = request.json
    filename = data.get('file', '')
    content = data.get('content', '')
    
    file_path = os.path.join(state.current_dir, filename)
    
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        log_message(f"File saved: {filename}", "FILE")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/delete', methods=['POST'])
def api_delete():
    """Delete file"""
    data = request.json
    filename = data.get('file', '')
    
    file_path = os.path.join(state.current_dir, filename)
    
    try:
        if os.path.isdir(file_path):
            shutil.rmtree(file_path)
        else:
            os.remove(file_path)
        log_message(f"Deleted: {filename}", "FILE")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/upload', methods=['POST'])
def api_upload():
    """Upload file"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': 'No file provided'})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': 'No file selected'})
    
    filename = secure_filename(file.filename)
    file_path = os.path.join(state.upload_dir, filename)
    
    try:
        file.save(file_path)
        log_message(f"File uploaded: {filename}", "UPLOAD")
        return jsonify({'success': True, 'filename': filename})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/uploads')
def api_uploads():
    """Get list of uploaded files"""
    files = []
    for item in os.listdir(state.upload_dir):
        full_path = os.path.join(state.upload_dir, item)
        if os.path.isfile(full_path):
            size = os.path.getsize(full_path)
            size_str = f"{size/1024:.1f} KB"
            files.append({'name': item, 'size': size_str})
    
    return jsonify({'files': files})

@app.route('/api/processes')
def api_processes():
    """Get running processes"""
    processes = get_all_processes()
    return jsonify({'processes': processes})

@app.route('/api/logs')
def api_logs():
    """Get system logs"""
    return Response('\n'.join(state.logs), mimetype='text/plain')

@app.route('/api/status')
def api_status():
    """Get system status"""
    return jsonify({
        'current_dir': state.current_dir,
        'process_count': len(state.processes),
        'logs_count': len(state.logs)
    })

@app.route('/api/run_uploaded', methods=['POST'])
def api_run_uploaded():
    """Run uploaded file"""
    data = request.json
    filename = data.get('filename', '')
    file_path = os.path.join(state.upload_dir, filename)
    
    if not os.path.exists(file_path):
        return jsonify({'success': False, 'message': 'File not found'})
    
    # Copy to current directory and run
    shutil.copy(file_path, state.current_dir)
    target_file = os.path.join(state.current_dir, filename)
    
    cmd = f"python3 {filename}" if filename.endswith('.py') else f"bash {filename}"
    
    return jsonify({
        'success': True,
        'message': f'File ready to run: {cmd}',
        'command': cmd
    })

# ==================== MAIN ====================
def open_browser():
    """Open browser automatically"""
    time.sleep(1)
    try:
        webbrowser.open('http://localhost:5000')
    except:
        pass

if __name__ == '__main__':
    print("\n" + "="*60)
    print("🚀 Termux Web Terminal Pro v1.0")
    print("="*60)
    print(f"📁 Current Directory: {os.getcwd()}")
    print(f"🌐 Web Interface: http://localhost:5000")
    print(f"📂 Upload Directory: {state.upload_dir}")
    print("="*60)
    print("✨ Features:")
    print("  • All Termux commands supported")
    print("  • Real-time output streaming")
    print("  • File upload/download")
    print("  • Process management with /stop")
    print("  • Built-in file editor")
    print("  • Live logs display")
    print("="*60)
    print("\nStarting server...")
    
    # Start browser in background
    threading.Thread(target=open_browser, daemon=True).start()
    
    # Start Flask server
    try:
        app.run(
            host='0.0.0.0',
            port=5000,
            debug=False,
            threaded=True,
           use_reloader=False
        )
    except KeyboardInterrupt:
        print("\nShutting down server...")
        # Kill all running processes
        for pid in list(state.processes.keys()):
            kill_process_tree(pid)
        print("All processes terminated.")
        sys.exit(0)
