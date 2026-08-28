"""
Project Pantry Audit: Retail Shelf-Health Scoring Pipeline
Builds an automated ETL pipeline using pure Python structures.
"""

import csv
import json
from pathlib import Path
import requests

API_URL = "https://world.openfoodfacts.org/api/v2/search"
SCRAPE_URL = "https://www.sugar.org/blog/making-sense-of-added-sugars-on-the-new-nutrition-facts-label/"
USER_AGENT = "CourseCode_HagerSafwat/1.0 (hager@example.com)"


def fetch_daily_value_sugar() -> float:
    """Scrapes the FDA Daily Value for added sugars from sugar.org."""
    try:
        res = requests.get(SCRAPE_URL, headers={"User-Agent": USER_AGENT}, timeout=10)
        res.raise_for_status()
        anchor = "the Daily Value is"
        if anchor in res.text:
            idx = res.text.find(anchor) + len(anchor)
            snippet = res.text[idx:idx + 20].strip()
            return float(snippet.split()[0])
    except Exception as e:
        print(f"Scrape fallback triggered ({e}). Using standard 50.0g.")
    return 50.0


def parse_quantity_grams(raw_qty) -> float | None:
    """Extracts numeric values from free-text quantity fields."""
    if not raw_qty or not isinstance(raw_qty, str):
        return None
    
    running_str = ""
    has_decimal = False
    started = False
    
    for char in raw_qty:
        if char.isdigit():
            started = True
            running_str += char
        elif char == '.' and not has_decimal and started:
            has_decimal = True
            running_str += char
        elif started:
            break
            
    try:
        return float(running_str) if running_str else None
    except ValueError:
        return None


def run_pipeline():
    """Executes full pipeline: Fetch -> Clean -> Feature Eng -> Join -> Export."""
    # 1. Scraping & API Fetching
    daily_value_sugar_g = fetch_daily_value_sugar()
    
    params = {
        "categories_tags_en": "breakfast-cereals",
        "page_size": 100,
        "fields": "code,product_name,brands,quantity,categories_tags_en,countries_tags,ingredients_text,nutrition_grades,nutriments"
    }
    
    try:
        res = requests.get(API_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=15)
        res.raise_for_status()
        products = res.json().get("products", [])
    except requests.exceptions.RequestException as e:
        print(f"API Error: {e}")
        return

    # 2. Setup Paths & Cohort Filtering
    raw_dir, processed_dir = Path("data/raw"), Path("data/processed")
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    # Drop products with missing sugars_100g
    cohort = [p for p in products if p.get("nutriments", {}).get("sugars_100g") is not None]

    # Calculate Cohort Means for Imputation
    fibers = [float(p["nutriments"]["fiber_100g"]) for p in cohort if p.get("nutriments", {}).get("fiber_100g") is not None]
    proteins = [float(p["nutriments"]["proteins_100g"]) for p in cohort if p.get("nutriments", {}).get("proteins_100g") is not None]
    
    mean_fiber = sum(fibers) / len(fibers) if fibers else 0.0
    mean_protein = sum(proteins) / len(proteins) if proteins else 0.0

    # 3. Cleaning & Feature Engineering
    cleaned_records = []
    for r in cohort:
        nutr = r.get("nutriments", {})
        sugars_100g = float(nutr.get("sugars_100g", 0.0))
        
        # Feature 1: sugar_pct_dv
        sugar_pct_dv = (sugars_100g / daily_value_sugar_g) * 100.0
        
        # Feature 2: sugar_tier
        if sugar_pct_dv < 5.0:
            sugar_tier = "low"
        elif sugar_pct_dv < 20.0:
            sugar_tier = "moderate"
        else:
            sugar_tier = "high"
            
        # Target: high_sugar_flag
        high_sugar_flag = 1 if (sugars_100g / daily_value_sugar_g) >= 0.20 else 0
        
        # Quantity parsing & Imputation
        qty_g = parse_quantity_grams(r.get("quantity"))
        fib = nutr.get("fiber_100g")
        prot = nutr.get("proteins_100g")
        
        cleaned_records.append({
            "barcode": str(r.get("code", "")),
            "product_name": r.get("product_name", ""),
            "brands": r.get("brands", ""),
            "nutrition_grade": r.get("nutrition_grades", "missing"),
            "quantity_grams": qty_g,
            "sugars_100g": sugars_100g,
            "fiber_100g": float(fib) if fib is not None else mean_fiber,
            "proteins_100g": float(prot) if prot is not None else mean_protein,
            "sugar_pct_dv": sugar_pct_dv,
            "sugar_tier": sugar_tier,
            "high_sugar_flag": high_sugar_flag
        })

    # 4. Min-Max Scaling
    pct_vals = [rec["sugar_pct_dv"] for rec in cleaned_records]
    min_x, max_x = min(pct_vals), max(pct_vals)
    denom = max_x - min_x if max_x != min_x else 1.0
    
    for rec in cleaned_records:
        rec["sugar_pct_dv_scaled"] = (rec["sugar_pct_dv"] - min_x) / denom

    # 5. Join with Warehouse Log (Dict Join)
    log_dict = {}
    log_file = raw_dir / "warehouse_scan_log.csv"
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                log_dict[str(row["barcode"])] = row

    for rec in cleaned_records:
        w_data = log_dict.get(rec["barcode"], {})
        rec["warehouse_status"] = w_data.get("status", "not_in_warehouse")
        rec["stock_count"] = w_data.get("stock_count", None)

    # 6. Validation Check Print
    grade_totals, grade_flagged = {}, {}
    for rec in cleaned_records:
        g = str(rec["nutrition_grade"]).lower()
        grade_totals[g] = grade_totals.get(g, 0) + 1
        if rec["high_sugar_flag"] == 1:
            grade_flagged[g] = grade_flagged.get(g, 0) + 1

    print("\n--- Validation Check Table ---")
    print("Grade | Total | Flagged | High Sugar Rate")
    for g in sorted(grade_totals.keys()):سس
        tot = grade_totals[g]
        flg = grade_flagged.get(g, 0)
        rate = (flg / tot) * 100.0 if tot > 0 else 0.0
        print(f"  {g.upper()}   |  {tot:2d}   |   {flg:2d}    | {rate:6.2f}%")

    # 7. Write to CSV
    output_path = processed_dir / "clean_data.csv"
    if cleaned_records:
        fieldnames = list(cleaned_records[0].keys())
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(cleaned_records)

    print(f"\nPipeline execution successful! Saved {len(cleaned_records)} records to {output_path}")


if __name__ == "__main__":
    run_pipeline()