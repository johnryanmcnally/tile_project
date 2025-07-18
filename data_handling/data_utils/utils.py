# Third Party Imports
import pandas as pd
import numpy as np
from sklearn.cluster import HDBSCAN

# Native Imports
from pathlib import Path
import json

# caching doesn't help here because we are not calling the function repeatedly with the same arguments
# @lru_cache(maxsize=None)
def combine_data(datapath: str, tile_uuid: str, tile_name: str):
    """
    Function to combine all data from the raw jsons to a dataframe, optimized for speed.

    Parameters
    -----------
    datapath : string
        the file path pointing to the directory holding the raw data in json format
    tile_uuid : string
        unique ID for the tile
    tile_name : string
        human readable name of the tile

    Returns
    ----------
    df : pd.DataFrame
        dataframe containing the combined data
    """
    all_location_updates = []
    files = Path(datapath).glob('*.json')

    for file in files:
        with open(file, 'r') as f:
            data = json.load(f)
            # Safely access nested data, handle potential missing keys if your JSONs vary
            if tile_uuid in data and 'result' in data[tile_uuid] and \
               'location_updates' in data[tile_uuid]['result']:
                all_location_updates.extend(data[tile_uuid]['result']['location_updates'])

    if not all_location_updates:
        return pd.DataFrame() # Return empty DataFrame if no data found

    df = pd.DataFrame(all_location_updates)

    # Convert 'location_timestamp' to datetime directly
    df['datetime'] = pd.to_datetime(df['location_timestamp'], unit='ms', utc=True)
    df['date'] = df['datetime'].dt.date
    df['time'] = df['datetime'].dt.strftime("%H:%M:%S")

    # Add tile_name column
    df['tile_name'] = tile_name

    # Add tile_uuid column
    df['tile_uuid'] = tile_uuid # Assuming tile_uuid is constant for all data in this combination process

    # Remove Duplicates
    df = df.groupby('location_timestamp', as_index=False).last()
    df = df.sort_values(by='datetime')

    # Ensure all columns exist before reordering to avoid KeyError
    required_cols = ['tile_name', 'tile_uuid', 'location_timestamp', 'datetime', 'date', 'time',
                     'latitude', 'longitude', 'raw_precision', 'precision']
    final_cols = [col for col in required_cols if col in df.columns]
    df = df[final_cols] # Reorder columns

    return df

# *** DEPRACATED - no longer used because caching is not necessary ***
def numpy_to_hashable_bytes(arr):
    """Converts a NumPy array to hashable bytes, including dtype and shape."""
    # how to rebuild the array:
    # coords = np.frombuffer(bytes_data, dtype=dtype).reshape(shape)
    return arr.tobytes(), arr.dtype, arr.shape

# caching doesn't help here because we are not calling the function repeatedly with the same arguments
# @lru_cache(maxsize=None)
def cluster_data(df, metric: str = 'haversine', min_cluster_size: int = 5):
    """
    Fits an HDBSCAN clustering model to the data and returns the labels

    Parameters
    -----------
    df : pd.DataFrame
        dataframe that contains the columns to be fit on (should only be ['latitude', 'longitude'])

    Returns
    -----------
    db : sklearn HDBSCAN class
        fit model
    db.labels_ : np.array
        array containing the labels
    """
    # scaler = StandardScaler()
    # coords = scaler.fit_transform(df)
    # consider using metric='haversine' in future versions, which also need to remove standard scaler
    coords_radians = np.deg2rad(df[['latitude', 'longitude']].values)
    db = HDBSCAN(metric=metric, min_cluster_size=min_cluster_size, n_jobs=-1).fit(coords_radians) 
    return db, db.labels_

def add_bearing_column(df):
    """
    Function to calculate bearing for a DataFrame using vectorized operations.

    Parameters
    -----------
    df : pd.DataFrame
        dataframe containing ['latitude','longitude']

    Returns
    ---------
    bearing : pd.Series
        pandas series containing the bearing for every row
    """
    # Create a copy to avoid SettingWithCopyWarning if df is a slice
    df_copy = df.copy()

    # Shift latitude and longitude to get previous points -- will create NaN value for first row
    df_copy['prev_latitude'] = df_copy['latitude'].shift(1)
    df_copy['prev_longitude'] = df_copy['longitude'].shift(1)

    # Convert degrees to radians for trigonometric functions
    lat1_rad = np.deg2rad(df_copy['prev_latitude'])
    lon1_rad = np.deg2rad(df_copy['prev_longitude'])
    lat2_rad = np.deg2rad(df_copy['latitude'])
    lon2_rad = np.deg2rad(df_copy['longitude'])

    # Calculate differences in longitude
    dLon = lon2_rad - lon1_rad

    # Apply the bearing formula using vectorized NumPy functions
    y = np.sin(dLon) * np.cos(lat2_rad)
    x = np.cos(lat1_rad) * np.sin(lat2_rad) - np.sin(lat1_rad) * np.cos(lat2_rad) * np.cos(dLon)

    # Calculate bearing in radians, then convert to degrees
    brng_rad = np.arctan2(y, x)
    brng_deg = np.rad2deg(brng_rad)

    # Normalize bearing to be between 0 and 360 degrees
    bearing = (brng_deg + 360) % 360

    # Convert the NumPy array result back to a Pandas Series, preserving the original index
    return pd.Series(bearing, index=df.index)


def add_direction_similarity(df):
    """
    Function to calculate direction similarity for a DataFrame using vectorized operations.

    Parameters
    -----------
    df : pd.DataFrame 
        dataframe containing ['latitude','longitude']

    Returns
    -----------
    direction_similarity : pd.Series
        series containing the direction similarity for every row
    """
    cdf = df.copy()

    # Calculate differences for current and previous steps
    cdf['diff_lat'] = cdf['latitude'] - cdf['latitude'].shift(1)
    cdf['diff_lon'] = cdf['longitude'] - cdf['longitude'].shift(1)
    # Shift these differences to get 'previous' differences
    cdf['prev_diff_lat'] = cdf['diff_lat'].shift(1)
    cdf['prev_diff_lon'] = cdf['diff_lon'].shift(1)

    # Stack the columns to form 2D arrays where each row is a vector
    vector_A = cdf[['prev_diff_lat', 'prev_diff_lon']].values
    vector_B = cdf[['diff_lat', 'diff_lon']].values

    dot_product = np.sum(vector_A * vector_B, axis=1)
    magnitude_A = np.sqrt(np.sum(vector_A**2, axis=1))
    magnitude_B = np.sqrt(np.sum(vector_B**2, axis=1))

    # Calculate direction similarity
    direction_similarity = np.divide(dot_product,
                                     (magnitude_A * magnitude_B),
                                     out = np.zeros_like(dot_product, dtype=float), # Output array for results
                                     where = (magnitude_A * magnitude_B) != 0) # handle division by 0
    
    return pd.Series(direction_similarity, index=df.index)


def reduce_clusters(df: pd.DataFrame) -> pd.DataFrame:
    """
    Optimizes cluster labels based on direction similarity criteria.

    Parameters
    ----------
    df : pd.DataFrame
        dataFrame containing 'cluster_label' and 'direction_similarity' columns.

    Returns
    -------
    cluster_labels : pd.Series
        series containing the reduced cluster labels
    """
    cdf = df.copy()

    # Create a boolean mask for valid clusters (not -1 or -2)
    valid_cluster_mask = ~cdf['cluster_label'].isin([-1, -2])

    # Calculate mean of direction_similarity for clusters
    cluster_means = cdf[valid_cluster_mask].groupby('cluster_label')['direction_similarity'].mean()

    # Identify clusters that meet the condition
    clusters_to_relabel = cluster_means[cluster_means > 0.25].index.tolist()

    # Relabel these clusters to -3
    cdf.loc[cdf['cluster_label'].isin(clusters_to_relabel), 'cluster_label'] = -3

    cluster_labels = cdf['cluster_label']
    return cluster_labels
