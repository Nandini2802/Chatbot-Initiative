import requests, json, warnings
warnings.filterwarnings("ignore")

KEY = "hffghfgVb$)7yV;e0N<{e1t@,_S{_LNovryojjUpJTO#-sD4gE*HHcpBX(Q%KEK6ATy6lQK"
URL = "https://middleware.danubeproperties.com/api/marketing_collateral"

resp = requests.post(URL, headers={"x_api_key": KEY, "Content-Type": "application/json"})
items = resp.json().get("data", {}).get("marketingCollateral", [])
greenz = next((i for i in items if "greenz" in i.get("project_title", "").lower()), None)

if greenz:
    print("project_title:", greenz.get("project_title"))
    du = greenz.get("download_urls", {})
    print("download_urls top keys:", list(du.keys()))
    folders = du.get("folders", {})
    print("folder keys:", list(folders.keys()))
    for k, v in folders.items():
        url = v.get("url", "") if isinstance(v, dict) else str(v)
        print(f"  {k}: {url}")

    # Test interiors download URL
    iurl = folders.get("interiors", {}).get("url", "")
    if iurl:
        print("\nTesting interiors URL...")
        r = requests.get(iurl, allow_redirects=True, timeout=15)
        print("  status:", r.status_code)
        print("  content-type:", r.headers.get("Content-Type"))
        print("  content-disposition:", r.headers.get("Content-Disposition"))
        print("  final url:", r.url[:120])
else:
    print("Greenz not found")
    print("Available projects:", [i.get("project_title") for i in items[:5]])
