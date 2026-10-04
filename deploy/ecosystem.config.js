module.exports = {
  apps: [{
    name: "bsky-agent",
    script: "main.py",
    args: "run",
    interpreter: "python3",
    cwd: "/opt/bsky_agent",
    restart_delay: 20000,
    max_restarts: 1000,
    min_uptime: "30s",
    autorestart: true,
    watch: false,
    max_memory_restart: "400M",
    out_file: "/opt/bsky_agent/data/pm2-out.log",
    error_file: "/opt/bsky_agent/data/pm2-err.log",
    merge_logs: true,
    kill_timeout: 30000,
    env: { PYTHONUNBUFFERED: "1" }
  }]
};
