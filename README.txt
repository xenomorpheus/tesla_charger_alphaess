API Documentation
https://www.teslaapi.io/vehicles/commands

Good python with CLI
https://github.com/tdorssers/TeslaPy

-------------------
Original setup

git clone ......
cd tesla-alphaess
git submodule update --init

# Create Python virtial environment, and install required packages.
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# Now we need to get Tesla access token. Will be stored in cache.json
cd TeslaPy/
python menu.py
# Use your Tesla credentials. A config.json will be created.
cp cache.json ..
cd ..

# Set your Tesla and Alphaess credentials
cp config-dist.json config.json

-------------------
Running the charging script

# If you haven't done so already, start the Python vertial environment
. .venv/bin/activate

# Run the charging script
./tesla_charger_alphaess.py

# If you want to upgrade outdated packates. The update the required packages.
pip --disable-pip-version-check list --outdated --format=json | python -c "import json, sys; print('\n'.join([x['name'] for x in json.load(sys.stdin)]))" | xargs -n1 pip install -U
