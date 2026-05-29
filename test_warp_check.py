import subprocess

def run_command(cmd, shell=True):
    try:
        result = subprocess.run(
            cmd,
            shell=shell,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        return -1, "", str(e)

code, output, err = run_command('netsh interface ipv4 show interfaces')
print(f"Return code: {code}")
print(f"Output length: {len(output)}")
print(f"Contains CloudflareWARP: {'CloudflareWARP' in output}")
print(f"Contains connected: {'connected' in output}")
print("First 200 chars of output:")
print(output[:200])
