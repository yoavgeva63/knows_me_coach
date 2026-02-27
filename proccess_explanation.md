***Setting Up***

**In the local computer:**
venv\Scripts\activate

**Setting the remote server:**
Goal: Host the Telegram bot on a free, always-running server.
Server: Oracle Cloud Free Tier — VM.Standard.E2.1.Micro (1 OCPU, 1GB RAM, Ubuntu 22.04), permanently free.

Connecting from my local machine:
    cd "C:\Users\gevay\Desktop\programing\my_coach_agent\ssh_keys"
    ssh -i private-ssh-key.key ubuntu@129.159.141.62

After connecting, deploying updates:
    cd knows_me_coach
    git pull
    sudo systemctl restart knows_me_coach

Checking if bot is running:
    sudo systemctl status knows_me_coach

Viewing live logs (for debugging):
    sudo journalctl -u knows_me_coach -f

Stopping the bot:
    sudo systemctl stop knows_me_coach

Starting the bot:
    sudo systemctl start knows_me_coach

When wanting to run Python commands directly: 
    source venv/bin/activate