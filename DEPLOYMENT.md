# Deployment Guide: Setting Up DamxdMail Publicly on an RDP Server

Follow this guide step-by-step to deploy your DamxdMail project on a Windows RDP server and make it publicly accessible on the internet under your custom domain (e.g., `damxd.shop`).

---

## Prerequisites

1. **Windows RDP Server** with a static public IP address.
2. **Domain Name** (e.g., `damxd.shop`) registered on Cloudflare, Namecheap, GoDaddy, etc.
3. **Python 3.10+** installed on the RDP server.

---

## Step 1: Configure DNS Records (Crucial for Emails)

You must point your domain to the RDP server's IP address and specify that your server handles incoming mail. 

Log in to your domain registrar or DNS manager (like Cloudflare) and add the following records:

| Record Type | Host / Name | Value / Target | TTL | Proxy Status (Cloudflare) |
| :--- | :--- | :--- | :--- | :--- |
| **A** | `@` (Root) | `YOUR_RDP_PUBLIC_IP` | Auto / 1 Hour | **DNS Only (Grey Cloud)** |
| **A** | `*` (Wildcard) | `YOUR_RDP_PUBLIC_IP` | Auto / 1 Hour | **DNS Only (Grey Cloud)** |
| **MX** | `@` (Root) | `damxd.shop` (your domain) | Auto | N/A (Priority: `10`) |
| **TXT** | `@` (Root) | `v=spf1 ip4:YOUR_RDP_PUBLIC_IP -all` | Auto | N/A (SPF Record) |

> [!WARNING]
> If you are using Cloudflare, you **MUST** set the A records to **DNS Only** (Grey Cloud), not Proxied (Orange Cloud). Cloudflare does not proxy raw SMTP traffic (Port 25), and proxying the A record will prevent the MX record from resolving to your server.

---

## Step 2: Open Firewall Ports on the RDP Server

Windows Server blocks incoming connections on ports 25 and 5000 by default. You need to allow inbound traffic on these ports.

1. Open **PowerShell** as **Administrator** on the RDP.
2. Run the following commands to create inbound rules in the Windows Defender Firewall:

```powershell
# Open SMTP port (25) to receive incoming emails
New-NetFirewallRule -DisplayName "Temp Mail SMTP Inbound" -Direction Inbound -LocalPort 25 -Protocol TCP -Action Allow

# Open Web Port (5000) for the Flask interface
New-NetFirewallRule -DisplayName "Temp Mail Web Inbound" -Direction Inbound -LocalPort 5000 -Protocol TCP -Action Allow
```

> [!IMPORTANT]
> If your RDP provider (such as AWS, Azure, Google Cloud, Oracle Cloud, or Contabo) has an external Network Security Group / Firewall in their dashboard, you must also allow inbound TCP traffic on ports **25** and **5000** (or **80** / **443** if using a reverse proxy) in their management console.

---

## Step 3: Set Up and Run the Project

1. Copy the project folder to the RDP server.
2. Open **Command Prompt** or **PowerShell** in the project directory.
3. Install the Python dependencies:
   ```cmd
   pip install -r requirements.txt
   ```
4. Verify your `.env` configuration file contains:
   ```env
   CUSTOM_DOMAIN=damxd.shop
   DB_PATH=temp_mail.db
   SMTP_HOST=0.0.0.0
   SMTP_PORT=25
   API_PORT=5000
   SECRET_KEY=your-random-secret-key-here
   ```
5. Start the application:
   ```cmd
   python main.py
   ```

---

## Step 4: Run as a Persistent Windows Service (Recommended)

To ensure the application keeps running after you close the RDP connection, or starts automatically when the server reboots:

### Option A: Use NSSM (Non-Sucking Service Manager)
1. Download **NSSM** (from [nssm.cc](https://nssm.cc/)) and extract it on the RDP.
2. Open PowerShell as Administrator and run:
   ```cmd
   nssm install DamxdMailService
   ```
3. A GUI window will open. Configure it as follows:
   - **Path**: Path to your `python.exe` (e.g., `C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe` or `C:\Windows\py.exe`).
   - **Startup directory**: Path to your project folder (e.g., `C:\temp_mail`).
   - **Arguments**: `main.py`
4. Click **Install Service**.
5. Start the service by running:
   ```cmd
   nssm start DamxdMailService
   ```

### Option B: Use Docker
If you have Docker Desktop (with WSL2) installed on the RDP:
1. Build the image:
   ```cmd
   docker build -t damxd-mail .
   ```
2. Run the container in detached (background) mode:
   ```cmd
   docker run -d --name damxd-mail-app -p 5000:5000 -p 25:25 -v C:\temp_mail_data:/data --restart always damxd-mail
   ```

---

## Step 5: (Optional) Set Up Port 80/443 (HTTPS)

Running on port `5000` is fine, but for a professional look, you want users to access it directly on standard web ports (`80` or `443` with SSL).

### Quickest Method: Use Caddy Server (Automatic SSL)
1. Download **Caddy** for Windows (from [caddyserver.com](https://caddyserver.com/)).
2. Create a file named `Caddyfile` in the Caddy directory with the following content:
   ```caddy
   damxd.shop {
       reverse_proxy localhost:5000
   }
   ```
3. Run Caddy. It will automatically acquire Let's Encrypt SSL certificates for your domain and proxy port 80/443 traffic to your Flask app running on `5000`.
