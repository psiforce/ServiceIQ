"""Fixed emergency safety messages, returned without retrieval or generation.

The wording follows the Emergency Safety Guide (crc-guide-emergency-safety) and must be reviewed
by each tenant's safety manager before go-live. Company contact details come from the tenant profile.
Scripts are fixed text on purpose: in an emergency the customer gets the same vetted guidance every
time, instantly, with no chance of an LLM improvising troubleshooting steps.
"""
from serviceiq.tenants import TenantProfile

SCRIPTS = {
    "gas": (
        "If you smell gas, leave the building now with everyone, including pets. Don't switch lights, "
        "appliances or the thermostat on or off, and don't use your phone until you're outside. From a safe "
        "distance, call your gas utility's emergency number or 911. Don't go back in until they say it's safe."
    ),
    "carbon_monoxide": (
        "Get everyone outside into fresh air right now and call 911. Don't go back inside until emergency "
        "responders say it's safe. Never disable or silence a carbon monoxide alarm that is sounding."
    ),
    "fire_smoke": (
        "If you see smoke, flames or sparks, leave the home now and call 911. If it's safe to do so on your way "
        "out, turn the system off at the thermostat. Don't go back in until firefighters say it's safe."
    ),
    "burning_smell": (
        "Turn the system off at the thermostat and, if it's safe, switch off the furnace power switch or the "
        "breaker for the equipment. If you see smoke or flames, leave the home and call 911."
    ),
    "electrical_water": (
        "Turn the system off at the thermostat and stay away from wet outlets, wiring and electrical parts. "
        "If you can reach the breaker without standing in water, switch it off; otherwise keep clear and call 911 "
        "if there is any sparking or smoke. Move belongings away from the water if it's safe."
    ),
    "no_heat_extreme": (
        "No heat in freezing weather is treated as an emergency. Never use an oven, stove, grill or generator "
        "to heat your home: they can cause carbon monoxide poisoning or fire. Use space heaters only as the "
        "manufacturer directs and keep them away from anything flammable."
    ),
    "unsafe_heating": (
        "Please don't use an oven, stove, grill or generator to heat your home: they can cause carbon monoxide "
        "poisoning or fire. Use space heaters only as the manufacturer directs and keep them away from anything "
        "flammable."
    ),
    "no_cool_extreme": (
        "No cooling in extreme heat is treated as an emergency, especially for elderly people, infants or anyone "
        "with medical needs. Move to the coolest room or a cooled public space, drink water, and call 911 if "
        "anyone shows signs of heat illness such as confusion, fainting or a very high temperature."
    ),
}

# Emergencies the HVAC company itself should respond to (after first responders, where relevant)
AFTER_FIRST_RESPONDERS = {"gas", "carbon_monoxide", "fire_smoke"}


def emergency_message(kind: str, profile: TenantProfile) -> str:
    script = SCRIPTS[kind]
    if profile.emergency_phone:
        when = "Once everyone is safe and responders have cleared the home, " if kind in AFTER_FIRST_RESPONDERS else ""
        contact = (f"{when}call the {profile.company_name} 24/7 emergency line at {profile.emergency_phone} "
                   f"so a technician can inspect your equipment.")
    else:
        contact = (f"{profile.company_name} does not offer 24/7 emergency service. Call 911 if anyone is in danger; "
                   f"otherwise call {profile.office_phone} during business hours ({profile.business_hours}).")
    return f"{script}\n\n{contact[0].upper()}{contact[1:]}"
