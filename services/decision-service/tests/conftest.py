import os

# unit and service tests always use the deterministic stub: no network,
# no API key, no dependence on the developer's shell configuration
os.environ["RECOMMENDATION_PROVIDER"] = "stub"
