import pandas as pd
import os

import pandas as pd
import os
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE


def convert_semicolon_csv_to_excel(csv_file_path, excel_file_path=None):
    if not os.path.isfile(csv_file_path):
        raise FileNotFoundError(f"CSV file not found: {csv_file_path}")

    # Read CSV with semicolon delimiter and no header
    df = pd.read_csv(csv_file_path, sep=';', header=None)

    # Define initial expected column names
    column_names = ['picture', 'X', 'Y', 'Z']

    # Add generic names for remaining columns
    remaining = len(df.columns) - len(column_names)
    if remaining > 0:
        column_names += [f"unnamed_{i+1}" for i in range(remaining)]

    # Assign column names
    df.columns = column_names[:len(df.columns)]
    
    # Clean illegal characters from every cell
    df = df.applymap(lambda x: ILLEGAL_CHARACTERS_RE.sub("", x) if isinstance(x, str) else x)

    # Generate Excel file name if not provided
    if not excel_file_path:
        base = os.path.splitext(csv_file_path)[0]
        excel_file_path = f"{base}.xlsx"

    # Save to Excel
    df.to_excel(excel_file_path, index=False)

    print(f"Converted: {csv_file_path} → {excel_file_path}")

# Example usage
csv_path = r"/home/administrator/Street_View/geovisio/fgic_streetview_data/Leica_Topcon/Leica-2019-JUL-11_AbuDhabiUpdate/panorama1/import/import_locations.csv"
excel_path = r"/home/administrator/Street_View/geovisio/fgic_streetview_data/Leica_Topcon/Leica-2019-JUL-11_AbuDhabiUpdate/panorama1/import/output.xlsx"
convert_semicolon_csv_to_excel(csv_path, excel_path)
