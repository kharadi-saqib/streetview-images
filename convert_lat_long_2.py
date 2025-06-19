# import os
# import requests
# import pandas as pd
# from time import sleep
# from pyproj import Transformer


# def convert_to_lat_long(df):
#     # Transformer to convert from EPSG:3857 (X, Y) to EPSG:4326 (Lat, Lon)
#     transformer = Transformer.from_crs("EPSG:32640", "EPSG:4326", always_xy=True)
#     latitudes = []
#     longitudes = []
    
#     for index, row in df.iterrows():
#         x = row['X']
#         y = row['Y']

       
        
#         # Transform X, Y to Longitude, Latitude
#         lon, lat = transformer.transform(x, y)
        
#         latitudes.append(lat)
#         longitudes.append(lon)    
    
#     # Add the converted coordinates to the DataFrame
#     df['override_latitude'] = latitudes
#     df['override_longitude'] = longitudes

#     return df

# def main(input_file, output_file):
#     # Read Excel file
#     df = pd.read_excel(input_file)
    
#     # Convert to latitude and longitude
#     df_converted = convert_to_lat_long(df)
    
#     # Save to a new Excel file
#     df_converted.to_excel(output_file, index=False)
#     print(f"Converted file saved as: {output_file}")


# if __name__ == "__main__":
#     # Example usage
#     input_file = r"C:\Users\Dell\Downloads\input_file.xlsx"
#     output_file = r"C:\Users\Dell\Downloads\output5.xlsx"
#     main(input_file, output_file)



import os
import requests
import pandas as pd
from time import sleep
from pyproj import Transformer
from datetime import datetime
import re




# def get_override_capture_time(base_path):
#     # Extract the date part from base_path (e.g., 2019-JUL-11)
#     match = re.search(r"\d{4}-[A-Za-z]{3}-\d{2}", base_path)
#     if not match:
#         raise ValueError("No valid date in format YYYY-MMM-DD found in base_path.")
    
#     date_part = match.group()  # e.g., '2019-JUL-11'
#     date_obj = datetime.strptime(date_part, "%Y-%b-%d").date()  # parse to date

#     now = datetime.now()
#     return datetime.combine(date_obj, now.time())  # combine with current time


# def get_override_capture_time(base_path):
#     match = re.search(r"\d{4}-[A-Za-z]{3}-\d{2}", base_path)
#     if not match:
#         raise ValueError("No valid date in format YYYY-MMM-DD found in base_path.")
    
#     date_part = match.group()
#     date_obj = datetime.strptime(date_part, "%Y-%b-%d").date()
#     now = datetime.now()
#     combined = datetime.combine(date_obj, now.time())
#     return combined.isoformat()  # return as ISO string

import re
from datetime import datetime, timezone

def get_override_capture_time(base_path):
    match = re.search(r"\d{4}-[A-Za-z]{3}-\d{2}", base_path)
    
    if match:
        date_part = match.group()
        date_obj = datetime.strptime(date_part, "%Y-%b-%d").date()
        now = datetime.now()
        combined = datetime.combine(date_obj, now.time())
        return combined.isoformat()
    else:
        # Fallback: current UTC time in ISO format
        return datetime.now(timezone.utc).isoformat()




def convert_to_lat_long(df):
    # Transformer to convert from EPSG:3857 (X, Y) to EPSG:4326 (Lat, Lon)
   # transformer = Transformer.from_crs("EPSG:32639", "EPSG:4326", always_xy=True)
   # transformer = Transformer.from_crs("EPSG:32638", "EPSG:4326", always_xy=True)
    transformer = Transformer.from_crs("EPSG:32640", "EPSG:4326", always_xy=True)
    
    latitudes = []
    longitudes = []
    
    # for index, row in df.iterrows():
    #     x = row['X']
    #     y = row['Y']

    for index, row in df.iterrows():
        x = row['Y']
        y = row['Z']
               
        # Transform X, Y to Longitude, Latitude
        lon, lat = transformer.transform(x, y)
        
        latitudes.append(lat)
        longitudes.append(lon)    
    
    # Add the converted coordinates to the DataFrame
    df['override_latitude'] = latitudes
    df['override_longitude'] = longitudes

    if 'picture' in df.columns:
    # Extract the last part of the original picture path as picture_name
        df['picture_name'] = df['picture'].astype(str).apply(os.path.basename)

        # Then normalize and update picture with base_path
        df['picture'] = df['picture'].astype(str).apply(
            lambda pic: os.path.normpath(os.path.join(base_path, os.path.basename(pic)))
        )

    # Add override_capture_time column
    # Add override_capture_time column
    override_time = get_override_capture_time(base_path)
    df['override_capture_time'] = override_time

     # Add position column starting from 1
    df['position'] = range(1, len(df) + 1)

    return df
    

def main(input_file, output_file,base_path):
    # Read Excel file
    df = pd.read_excel(input_file)
    
    # Convert to latitude and longitude
    df_converted = convert_to_lat_long(df)
    
    # Save to a new Excel file
    df_converted.to_excel(output_file, index=False)
    print(f"Converted file saved as: {output_file}")

if __name__ == "__main__":
    # Example usage
   # input_file = r"C:\Users\Dell\Downloads\input_file.xlsx"
    #output_file = r"C:\Users\Dell\Downloads\output14.xlsx"
    # input_file=r"C:\Users\Dell\Downloads\import_locations-TMX9318072302-000130.xlsx"
    # output_file=r"C:\Users\Dell\Downloads\import_locations-TMX9318072302-000130_output.xlsx"
    input_file=r"C:\Users\Dell\Downloads\import_locations-Leica-2018-DEC-04_ADSwehanHighway.xlsx"
    output_file=r"C:\Users\Dell\Downloads\output_5.xlsx"
    base_path = r"D:\Download_folder_data\street_view_data\For_CodeRize_StreetView\Trimble\TMX9318072302-000143\panorama1\original"  # Update this as needed
    main(input_file, output_file,base_path)

