import { execFile } from 'node:child_process';

// Windows virtualenv launchers may own another Python process. Stop only the
// fixture process tree created by this test, before deleting its SQLite files.
export async function stopTestProcess(child) {
  if (!child?.pid || child.exitCode !== null || child.signalCode !== null) return;
  const closed = new Promise(resolve => child.once('close', resolve));
  if (process.platform === 'win32') {
    await new Promise((resolve, reject) => {
      execFile('taskkill', ['/PID', String(child.pid), '/T', '/F'], error => {
        if (error && child.exitCode === null && child.signalCode === null) reject(error);
        else resolve();
      });
    });
  } else {
    child.kill('SIGTERM');
  }
  await closed;
}
