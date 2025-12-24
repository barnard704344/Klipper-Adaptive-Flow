# Print Analysis

After each print, you can get AI-powered suggestions to improve your settings. The AI looks at your print data and tells you what to adjust.

---

## Quick Setup (5 minutes)

### Step 1: Get a Free API Key

The easiest option is **GitHub Models** (free):

1. Go to https://github.com/settings/tokens
2. Click "Generate new token (classic)"
3. Give it a name like "Adaptive Flow"
4. Don't check any boxes - no permissions needed
5. Click "Generate token"
6. Copy the token (starts with `ghp_`)

### Step 2: Add Your Key

Edit the file `analysis_config.cfg` in your Klipper-Adaptive-Flow folder:

```ini
[analysis]
provider: github
api_key: ghp_paste_your_token_here
```

### Step 3: Run Analysis

After a print completes:

```bash
cd ~/Klipper-Adaptive-Flow
python3 analyze_print.py
```

That's it! You'll see suggestions like:

```
💡 Suggestions (2):

  1. speed_boost_k
     Current: 0.08 → Suggested: 0.10
     Reason: Better heat at high speed
     [⚠ MANUAL]

  2. ramp_rate_fall  
     Current: 1.0 → Suggested: 1.5
     Reason: Smoother temperature transitions
     [✓ SAFE]
```

### Understanding Suggestion Types

| Tag | Meaning | Action |
|-----|---------|--------|
| **[✓ SAFE]** | Conservative change, low risk | Can be auto-applied |
| **[⚠ MANUAL]** | Significant change | Review and apply manually |

Safe changes are small adjustments that won't cause print failures. Manual changes are larger or affect critical parameters.

---

## Auto-Apply Safe Suggestions

To automatically apply safe suggestions after analysis:

```ini
[analysis]
auto_apply: true
```

When enabled:
- **[✓ SAFE]** suggestions → Applied to your config automatically
- **[⚠ MANUAL]** suggestions → Shown for you to review

---

## Other Providers

If you prefer a paid provider:

**OpenAI / ChatGPT** (paid):
```ini
[analysis]
provider: openai
api_key: sk-your-openai-key
```

**Anthropic Claude** (paid):
```ini
[analysis]
provider: anthropic
api_key: sk-ant-your-anthropic-key
```

---

## Auto-Analyze Every Print

Want analysis to run automatically after each print? Set it up as a background service.

### Step 1: Make Sure Your Config is Set

Edit `analysis_config.cfg` with your provider and API key (see above).

### Step 2: Create the Service

```bash
sudo nano /etc/systemd/system/adaptive-flow-hook.service
```

Paste this (change `pi` to your username if different):

```ini
[Unit]
Description=Adaptive Flow Print Analyzer
After=moonraker.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Klipper-Adaptive-Flow
ExecStart=/usr/bin/python3 /home/pi/Klipper-Adaptive-Flow/moonraker_hook.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Save with Ctrl+O, Enter, Ctrl+X.

### Step 3: Start It

```bash
sudo systemctl daemon-reload
sudo systemctl enable adaptive-flow-hook
sudo systemctl start adaptive-flow-hook
```

Now every print will be analyzed automatically!

### Check If It's Running

```bash
sudo systemctl status adaptive-flow-hook
```

---

## What Does the AI Analyze?

The AI looks at data from your print:

- **Temperature**: Did the heater keep up? Was it working too hard?
- **Flow Rate**: How much plastic was being pushed through
- **Print Speed**: How fast the toolhead was moving
- **Any Errors**: Problems in the Klipper log

Based on this, it suggests changes to make your prints better.

---

## Understanding the Suggestions

The AI might suggest changing values in your config. Here's what they mean:

| Setting | What It Does | When to Increase |
|---------|--------------|------------------|
| `flow_k` | Adds heat when pushing more plastic | Heater can't keep up during infill |
| `speed_boost_k` | Adds heat at high speeds | Underextrusion on fast moves |
| `ramp_rate_rise` | How fast temp goes up | Slow response to speed changes |
| `max_boost` | Maximum extra temperature | Need more heat headroom |

**Don't worry about memorizing these** - the AI explains why it's suggesting each change.

---

## Configuration Options

All settings are in `analysis_config.cfg`:

```ini
[analysis]
# Which AI service to use: github (FREE), openai, anthropic
provider: github

# Your API key
api_key: ghp_your_token

# Model to use (optional - uses provider default if blank)
model: 

# Automatically apply safe suggestions
auto_apply: false

# Show results in Klipper console
notify_console: true
```

---

## Troubleshooting

### "No logs found"

The analysis needs print data. Make sure you've completed at least one print with Adaptive Flow enabled.

Check if logs exist:
```bash
ls ~/printer_data/logs/adaptive_flow/
```

### "API key error" or "Unauthorized"

Your API key might be wrong. Double-check:
1. No extra spaces in `analysis_config.cfg`
2. Token hasn't expired (GitHub tokens can expire)
3. Token was copied completely

### Need More Help?

Open an issue on GitHub with:
1. The error message you see
2. Which provider you're using
3. Your `analysis_config.cfg` (remove your API key first!)
