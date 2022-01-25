#!/usr/bin/env python

"""
Charge Tesla vehicle using AlphaEss Inverter.
"""
import argparse
import asyncio
import json
import logging
import math
import pprint
import sys
import time
from typing import Optional

import numpy as np
from alphaess import alphaess  # type: ignore
from requests import HTTPError
from teslapy import Tesla, Vehicle, VehicleError  # type: ignore

try:
    import webview  # Optional pywebview 3.0 or higher
except ImportError:
    webview = None
try:
    from selenium import webdriver  # Optional selenium 3.13.0 or higher
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    webdriver = None

logger = logging.getLogger(__name__)
pp = pprint.PrettyPrinter(indent=4)


def __tesla_custom_auth(url):
    """Tesla authentication callback."""
    # Use pywebview if no web browser specified
    if webview and not (webdriver and args.web is not None):
        result = [""]
        window = webview.create_window("Login", url)

        def on_loaded():
            result[0] = window.get_current_url()
            if "void/callback" in result[0].split("?", maxsplit=1)[0]:
                window.destroy()

        window.loaded += on_loaded
        webview.start()
        return result[0]
    # Use selenium to control specified web browser
    with [webdriver.Chrome, webdriver.Edge, webdriver.Firefox, webdriver.Safari][args.web]() as browser:
        logger.debug("Selenium opened %s", browser.capabilities["browserName"])
        browser.get(url)
        WebDriverWait(browser, 300).until(EC.url_contains("void/callback"))
        return browser.current_url


def get_tesla_client(email: str) -> Tesla:
    """Get a connection to Tesla API."""
    if not args.verify:
        # Disable SSL verify for Nominatim
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        geopy.geocoders.options.default_ssl_context = ctx
    tesla_client: Tesla = Tesla(email, verify=args.verify, proxy=args.proxy, timeout=240)
    if (webdriver and args.web is not None) or webview:
        tesla_client.authenticator = __tesla_custom_auth
    return tesla_client


class TeslaEv:
    """Tesla EV."""

    def __init__(self, config: dict) -> None:
        """init"""
        self.config = config
        self.charge_amps_min: int = 1  # Tesla charging is inefficient at low amps
        self.communication_timeout: int = 240

        # Tesla
        self.tesla_client: Tesla = get_tesla_client(self.config["auth_email"])
        self.tesla_client.timeout = self.communication_timeout
        self.ev: Vehicle = self.tesla_client.vehicle_list()[self.config["vehicle_idx"]]

    def __del__(self):
        """Destructor."""
        # Safety measure to handle unexpected application termination
        if self.ev is not None:
            self.ev.command("STOP_CHARGE")
            self.ev = None

        # Tesla car UI will crash if we called charge_data() and exit before
        # calling close.
        if self.tesla_client is not None:
            self.tesla_client.close()
            self.tesla_client = None

    @classmethod
    def is_able_to_charge(cls, charge_state: dict) -> Optional[str]:
        """Optionally returns the reason the ev can't be charged.
        Eg. charge_port_latch not Engaged."""

        if charge_state["not_enough_power_to_heat"] is not None:
            return f"not_enough_power_to_heat : {charge_state['not_enough_power_to_heat']}"
        if charge_state["charge_port_latch"] != "Engaged":
            return f"charge_port_latch : {charge_state['charge_port_latch']}"
        if not charge_state["charge_port_door_open"]:
            return f"charge_port_door_open : {charge_state['charge_port_door_open']}"
        return None

    @classmethod
    def report_charge_state_summary(cls, charge_state: dict) -> None:
        """Report to the user a summary of the charge state."""
        print("\nTesla:")
        km_from_miles: float = 1.60934
        km_range: float = charge_state["battery_range"] * km_from_miles
        soc_limit: float = charge_state["charge_limit_soc"]
        print(f"   Battery: {km_range:0.1f} km, {charge_state['battery_level']:0.1f}% (limit {soc_limit:d}%)")
        km_added: float = charge_state["charge_miles_added_rated"] * km_from_miles
        print(f"   Charge added: {km_added:0.1f} km, {charge_state['charge_energy_added']:0.2f} kw")
        time_to_full_charge: float = charge_state["time_to_full_charge"]
        if time_to_full_charge != 0:
            print(f"   Time to full charge: {time_to_full_charge:0.1f} hrs")

    def calculate_charger_amps_request(self, amps_delta: int, charge_state: dict) -> int:
        """Calculate amps for charging rate."""
        amps_request: int = charge_state["charger_actual_current"] + amps_delta

        # Validate charge rate
        if amps_request < 0:
            amps_request = 0
        else:
            if amps_request < self.charge_amps_min:
                amps_request = 0
        charge_current_request_max: int = charge_state["charge_current_request_max"]
        if amps_request > charge_current_request_max:
            logger.error(
                "charger_amps_request %s tried to exceed charge_current_request_max %s",
                amps_request,
                charge_current_request_max,
            )
            amps_request = charge_current_request_max
        return amps_request

    def get_charge_state(self, attempts_max: int = 3) -> dict:
        """Get the Tesla Vehicle's 'charge_state' data values. Has a re-try loop and basic error handling."""
        attempts_count: int = 0
        last_exception: HTTPError = None
        while attempts_count < attempts_max:
            try:
                attempts_count += 1
                vehicle_data: dict = self.ev.get_vehicle_data(endpoints="charge_state")
                return vehicle_data["charge_state"]
            except HTTPError as err:
                last_exception = err
                logger.warning(repr(err))
                status_code: int = err.response.status_code
                logger.debug("status_code: %d", status_code)
                if status_code == 408:  # vehicle is offline or asleep
                    logger.info("Attempting to wake Tesla Vehicle")
                    self.ev.sync_wake_up(timeout=self.communication_timeout)
                    time.sleep(5.0)
                elif status_code == 429:  # Too Many Requests
                    logger.critical(err)
                    time.sleep(20.0)
                else:
                    logger.error("Unknown HTTPError exception. Update this code to handle it")
                    logger.critical(err)
                    time.sleep(30.0)
        raise last_exception

    def report_and_change_charge_rate(self, amps_delta: int) -> str:
        """1) Report to the user a summary of the charge state.
        2) Change amps rate if not Charged.
        3_ Return the charging_state e.g Charged, Charging, or Stopped."""
        charge_state: dict = self.get_charge_state()
        logger.debug("charge_state: %s", pp.pformat(charge_state))
        self.report_charge_state_summary(charge_state)

        reason = self.is_able_to_charge(charge_state)
        if reason is not None:
            return reason

        if charge_state["charging_state"] == "Charged":
            return charge_state["charging_state"]

        charger_amps_request: int = self.calculate_charger_amps_request(amps_delta, charge_state)

        # Set charge rate
        print("")
        charger_actual_amps: int = charge_state["charger_actual_current"]
        if charger_actual_amps == charger_amps_request:
            print(f"Charging state remains {charge_state['charging_state']}, {charger_actual_amps} amps.")
            return charge_state["charging_state"]  # 'Stopped', 'Charging', Charged

        print(f"Requesting charge rate {charger_amps_request} amps, was {charger_actual_amps}.")
        self.ev.sync_wake_up(timeout=self.communication_timeout)
        self.ev.command("CHARGING_AMPS", charging_amps=charger_amps_request)
        if charger_amps_request == 0:
            self.ev.command("STOP_CHARGE")
            return "Stopped"
        if charger_actual_amps == 0:
            self.ev.command("START_CHARGE")
        return "Charging"


class AlphaEssInverter:
    """AlphaEss Inverter."""

    def __init__(self, config: dict) -> None:
        self.config = config

        self.inverter_volts: float = 240.0
        self.inverter_power_max: float

        self.inverter_serial = self.config["serial"]
        self.alphaess_client: alphaess = alphaess.alphaess(self.config["app_id"], self.config["app_secret"])

    async def private_async_init(self) -> None:
        """Handle any async initialisation"""

        # AlphaEss
        authenticated = await self.alphaess_client.authenticate()
        if not authenticated:
            logger.fatal("AlphaEss authentication failure, quitting")
        time.sleep(1.0)  # AlphaEss wants a delay between requests

        # Get the inverter rating
        units = await self.alphaess_client.getESSList()
        logger.debug("units: %s", pp.pformat(units))
        for unit in units:
            if unit.get("sysSn") == self.inverter_serial and unit.get("poinv") is not None:
                self.inverter_power_max = unit["poinv"] * 1000.0
                break
        else:
            raise ValueError(f"Failed to find AlphaEss unit with SSN {self.inverter_serial}")
        logger.debug("inverter_power_max: %0.2f", self.inverter_power_max)

    def volts(self) -> float:
        """the voltage the inverter outputs"""
        return self.inverter_volts

    async def available_watts(self, battery_charging_factor: float = 0.0) -> float:
        """The additional amount of power available from the inverter. The total of the feed-in and optionally some of
        the power that is being used to charge the battery.  The total is limited by the inverter's maximum output.

        battery_charging_factor 0.0-1.0. If the battery is being charged, how much of that power to add to the
          feed-in. Defaults to 0, which means only consider the fead-in"""

        last_power: dict = await self.alphaess_client.getLastPowerData(self.inverter_serial)
        logger.debug("last_power: %s", pp.pformat(last_power))

        battery_charging: float = -1 * last_power["pbat"]
        feed_in: float = -1 * last_power["pgridDetail"]["pmeterL1"]
        inverter_load: float = last_power["pload"]
        logger.info("Inverter battery_charging: %0.2f watts", battery_charging)
        logger.info("Inverter feed_in: %0.2f watts", feed_in)
        logger.info("inverter load: %0.2f watts", inverter_load)
        raw_available_watts: float = feed_in
        if battery_charging < 0:
            raw_available_watts += battery_charging
        else:
            raw_available_watts += battery_charging * battery_charging_factor

        # Any additional inverter output can't cause the inverter to exceed its max power,
        # so we consider the current inverter load.
        inverter_available_watts: float = min(raw_available_watts, self.inverter_power_max - inverter_load)

        self.report_home_power(last_power["soc"], inverter_available_watts)
        return inverter_available_watts

    @classmethod
    def report_home_power(cls, home_battery_soc: float, inverter_available_watts: float) -> None:
        """Report major properties of the inverter."""
        print("Inverter:")
        print(f"   Battery SoC: {home_battery_soc:0.1f}%")
        print(f"   Latest available power: {inverter_available_watts:0.1f} watts")


async def charge_loop(ev: TeslaEv, inverter: AlphaEssInverter) -> None:
    """
    Charge Tesla vehicle using home AlphaEss Inverter.

    Set the Tesla charge rate to consume only the power being sent to either
    the grid or charge the home battery, but limit to what the inverter can
    provide as AC.
    """

    # Constants
    loop_delay_active_charging: float = 60
    # Normal delay for reading available power while actively charging
    loop_delay_charge_change_settle: float = 90
    # Total for EV to change power consumption and inverter to report change
    loop_delay_ev_charged: float = 2 * 3600
    # Periodical check on charge level

    charging_sample_count: int = 3
    ev_error_count_max: int = 5

    print("Charge loop starting")
    available_watts_recent = np.array([], dtype=float)
    ev_error_count_consecutive: int = 0
    result: str = "unset"
    while ev_error_count_consecutive < ev_error_count_max:
        loop_sleep_time: float = loop_delay_active_charging
        avail: float = await inverter.available_watts()
        available_watts_recent = np.insert(available_watts_recent, 0, [avail])
        available_watts_recent = available_watts_recent[:charging_sample_count]
        logger.debug("available_watts_recent: %s", available_watts_recent)

        if result == "unset" or np.size(available_watts_recent) == charging_sample_count:
            watts_ave = np.average(available_watts_recent)
            print(f"   Ave. Inv available Power: {watts_ave:0.2f}")
            amps_delta: int = math.floor(watts_ave / inverter.volts())
            print(f"   Amps delta: {amps_delta}")
            if (
                result == "unset"
                or (result == "Charging" and amps_delta != 0)
                or (result == "Stopped" and amps_delta > 0)
            ):
                try:
                    result = ev.report_and_change_charge_rate(amps_delta)
                    loop_sleep_time = loop_delay_charge_change_settle
                    available_watts_recent = np.array([], dtype=float)
                    ev_error_count_consecutive = 0
                except VehicleError as ex1:
                    logger.error("VehicleError caught during report_and_change_charge_rate")
                    logger.critical(ex1)
                    ev_error_count_consecutive += 1
                    logger.error("ev_error_count_consecutive: %d", ev_error_count_consecutive)
            if result == "Charged":
                loop_sleep_time = loop_delay_ev_charged
            elif result == "Stopped":
                # TODO some kind of geometric back off
                loop_sleep_time = loop_delay_active_charging
            elif result == "Charging":
                loop_sleep_time = loop_delay_active_charging
            else:
                logger.error("Quitting. Unknown result: %s", result)
                break

        logger.debug("result: %s, sleeping for %02d", result, loop_sleep_time)
        time.sleep(loop_sleep_time)


async def main() -> None:
    """Script to control EV charger using available power"""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler("debug.log"), logging.StreamHandler()],
    )
    if args.debug:
        logger.setLevel(logging.DEBUG)

    with open("config.json", "r", encoding="utf-8") as myfile:
        data = myfile.read()
        config = json.loads(data)

    ev: TeslaEv = TeslaEv(config["tesla"])
    inverter: AlphaEssInverter = AlphaEssInverter(config["alphaess"])
    await inverter.private_async_init()
    await charge_loop(ev, inverter)
    print("main ending")


if __name__ == "__main__":
    # command line argument parser
    parser = argparse.ArgumentParser(description="Tesla Owner API Menu")
    parser.add_argument("-d", "--debug", action="store_true", help="set main module logging level to debug")
    parser.add_argument("--verify", action="store_false", help="disable verify SSL certificate")

    if webdriver:
        for c, s in enumerate(("chrome", "edge", "firefox", "safari")):
            d, h = (0, " (default)") if not webview and c == 0 else (None, "")
            parser.add_argument(
                "--" + s, action="store_const", dest="web", help=f"use {s.title() + h} browser", const=c, default=d
            )
    parser.add_argument("--proxy", help="proxy server URL")
    args = parser.parse_args()
    asyncio.run(main())
