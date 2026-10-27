import os
import re
import time
import datetime
import unicodedata
import pandas as pd
from thefuzz import process, fuzz
from nba_api.stats.endpoints import leaguegamelog, boxscoretraditionalv3

# --- STRATEGY 2: NAME NORMALIZATION & FUZZY MATCHING HELPERS ---

def normalize_name(name):
    """Strips accent marks/diacritics, lowercases, removes punctuation, and strips common suffixes."""
    if not isinstance(name, str) or not name.strip():
        return ""
    # 1. Strip accent marks / diacritics (e.g. Luka Dončić -> Luka Doncic)
    nfkd_form = unicodedata.normalize('NFKD', name)
    name_ascii = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    
    # 2. Lowercase and strip punctuation/symbols
    clean_name = re.sub(r'[^a-zA-Z0-9\s]', '', name_ascii).lower()
    
    # 3. Strip common suffixes
    suffixes = [' jr', ' sr', ' iii', ' ii', ' iv']
    for suffix in suffixes:
        if clean_name.endswith(suffix):
            clean_name = clean_name[:-len(suffix)]
            
    return clean_name.strip()


def resolve_player_dpm(nba_name, darko_norm_map, threshold=85):
    """
    Looks up player's scaled DPM from DARKO map using exact normalized match first, 
    then fuzzy string matching as fallback.
    """
    norm_nba_name = normalize_name(nba_name)
    if not norm_nba_name:
        return 0.0
    
    # 1. Exact match on normalized string
    if norm_nba_name in darko_norm_map:
        return darko_norm_map[norm_nba_name]
    
    # 2. Fuzzy match across normalized DARKO player names
    darko_names = list(darko_norm_map.keys())
    if not darko_names:
        return 0.0
        
    best_match, score = process.extractOne(
        norm_nba_name, 
        darko_names, 
        scorer=fuzz.token_sort_ratio
    )
    
    if score >= threshold:
        return darko_norm_map[best_match]
    
    return 0.0


# --- MAIN PIPELINE ---

def run_daily_injury_adjustment():
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    yesterday_str_api = yesterday.strftime('%Y-%m-%d')
    season = '2026-27'
    
    print(f"--- Running NBA Injury-Adjusted Wins Pipeline for {yesterday.strftime('%Y-%m-%d')} ---")
    
    # 1. Load DARKO DPM data & Shift DPM values so min(DPM) = 0
    darko_file = 'darko-dpm-leaderboard.csv'
    if not os.path.exists(darko_file):
        raise FileNotFoundError(f"Could not find '{darko_file}'.")
    
    darko_df = pd.read_csv(darko_file)
    
    dpm_col = 'DPM'
    min_dpm = darko_df[dpm_col].min()
    
    # Shift DPMs so the minimum DPM in the league is 0
    darko_df['dpm_scaled'] = darko_df[dpm_col] - min_dpm
    
    # Build DARKO dictionary keyed by normalized name
    darko_df['norm_name'] = darko_df['Player'].apply(normalize_name)
    darko_norm_map = darko_df.set_index('norm_name')['dpm_scaled'].to_dict()
    
    # 2. Fetch yesterday's team game logs
    team_logs = leaguegamelog.LeagueGameLog(
        season=season, 
        player_or_team_abbreviation='T', 
        season_type_all_star='Regular Season'
    ).get_data_frames()[0]
    print(f"Fetched {len(team_logs)} team game logs for the {season} season.")
    
    yesterday_games = team_logs[team_logs['GAME_DATE'] == yesterday_str_api]
    unique_game_ids = yesterday_games['GAME_ID'].unique()
    
    if len(unique_game_ids) == 0:
        print("No games played yesterday.")
        return
    
    print(f"Found {len(unique_game_ids)} games. Fetching box scores...")
    team_results = team_logs.set_index(['GAME_ID', 'TEAM_ID'])['WL'].to_dict()
    
    game_team_summaries = []
    injured_player_records = []
    
    for game_id in unique_game_ids:
        try:
            box = boxscoretraditionalv3.BoxScoreTraditionalV3(game_id=game_id)
            player_stats = box.get_data_frames()[0]
            
            rows = []
            for _, r in player_stats.iterrows():
                p_name = r.get('player_name', f"{r.get('firstName', '')} {r.get('familyName', '')}").strip()
                t_id = r.get('teamId', r.get('TEAM_ID'))
                t_abbrev = r.get('teamTricode', r.get('TEAM_ABBREVIATION', ''))
                comment = str(r.get('comment', r.get('COMMENT', ''))).strip()
                
                # Check injury rule: non-blank and NOT "DNP - Coach's Decision"
                is_injured = (comment != "") and (comment != "DNP - Coach's Decision") and (comment != "Inactive - Coach's Decision") and (comment != "Inactive - G League - Two-Way")
                
            
                # Resolve player DPM via Strategy 2 (Normalize + Fuzzy match)
                scaled_dpm = resolve_player_dpm(p_name, darko_norm_map)

                if is_injured:
                    print(f"Game {game_id} | Team {t_abbrev} | Player {p_name} is injured: '{comment}' | Scaled DPM: {scaled_dpm:.2f}")
                
                rows.append({
                    'TEAM_ID': t_id,
                    'TEAM_ABBREVIATION': t_abbrev,
                    'PLAYER_NAME': p_name,
                    'COMMENT': comment,
                    'IS_INJURED': is_injured,
                    'DPM_SCALED': scaled_dpm
                })

            df_game_players = pd.DataFrame(rows)
            
            teams_in_game = df_game_players['TEAM_ID'].unique()
            if len(teams_in_game) != 2:
                continue  # Skip invalid or incomplete box scores
            
            team_stats = {}
            for t_id in teams_in_game:
                t_df = df_game_players[df_game_players['TEAM_ID'] == t_id]
                total_team_dpm = t_df['DPM_SCALED'].sum()

                injured_team_dpm = t_df[t_df['IS_INJURED']]['DPM_SCALED'].sum()
                pct_injured_dpm = (injured_team_dpm / total_team_dpm) if total_team_dpm > 0 else 0.0
                wl = team_results.get((str(game_id), int(t_id) if pd.notna(t_id) else t_id), 'N/A')
                
                team_abbrev = t_df['TEAM_ABBREVIATION'].iloc[0]
                team_stats[t_id] = {
                    'TEAM_ID': t_id,
                    'TEAM_ABBREVIATION': team_abbrev,
                    'WL': wl,
                    'PCT_INJURED_DPM': pct_injured_dpm
                }

                # Extract individual injured players and calculate their % of team DPM
                injured_df = t_df[t_df['IS_INJURED']]
                for _, inj_row in injured_df.iterrows():
                    player_pct_dpm = (inj_row['DPM_SCALED'] / total_team_dpm) if total_team_dpm > 0 else 0.0
                    
                    injured_player_records.append({
                        'GAME_DATE': yesterday.strftime('%Y-%m-%d'),
                        'GAME_ID': game_id,
                        'TEAM_ABBREVIATION': inj_row['TEAM_ABBREVIATION'],
                        'PLAYER_NAME': inj_row['PLAYER_NAME'],
                        'COMMENT': inj_row['COMMENT'],
                        'PLAYER_DPM_SCALED': round(inj_row['DPM_SCALED'], 4),
                        'PCT_OF_TEAM_DPM': round(player_pct_dpm, 4)
                    })
            
            # Calculate Adj Wins using H4:I4 Logic
            t1_id, t2_id = teams_in_game[0], teams_in_game[1]
            t1, t2 = team_stats[t1_id], team_stats[t2_id]
            
            for t_curr, t_opp in [(t1, t2), (t2, t1)]:
                actual_win = 1 if t_curr['WL'] == 'W' else 0
                
                if actual_win == 1:
                    # Winner Adj Win = 1 - Opponent Injured DPM %
                    adj_win = 1.0 - t_opp['PCT_INJURED_DPM']+ t_curr['PCT_INJURED_DPM']
                else:
                    # Loser Adj Win = Own Injured DPM %
                    #adj_win = t_curr['PCT_INJURED_DPM']
                    adj_win = 0 # Losers don't get any credit for their own injuries; they still lost
                
                game_team_summaries.append({
                    'GAME_DATE': yesterday.strftime('%Y-%m-%d'),
                    'GAME_ID': game_id,
                    'TEAM_ABBREVIATION': t_curr['TEAM_ABBREVIATION'],
                    'ACTUAL_WIN': actual_win,
                    'OWN_INJURED_DPM_PCT': round(t_curr['PCT_INJURED_DPM'], 4),
                    'OPP_INJURED_DPM_PCT': round(t_opp['PCT_INJURED_DPM'], 4),
                    'ADJUSTED_WIN': round(adj_win, 4)
                })
                
            time.sleep(0.5)
            
        except Exception as e:
            print(f"Error processing game {game_id}: {e}")
            continue

    df_daily_summary = pd.DataFrame(game_team_summaries)
    
    # 5. Append daily summary to running totals file
    running_file = 'running_injury_adjusted_wins.csv'
    if os.path.exists(running_file):
        df_running = pd.read_csv(running_file)
        df_combined = pd.concat([df_running, df_daily_summary], ignore_index=True)
    else:
        df_combined = df_daily_summary
        
    df_combined.to_csv(running_file, index=False)
    print(f"Updated '{running_file}' with yesterday's games. Total rows: {len(df_combined)}")

    # 6. Export / Append Injured Players
    df_daily_injured = pd.DataFrame(injured_player_records)
    running_injured_file = 'running_injured_players.csv'
    
    if os.path.exists(running_injured_file):
        df_running_injured = pd.read_csv(running_injured_file)
        df_combined_injured = pd.concat([df_running_injured, df_daily_injured], ignore_index=True)
    else:
        df_combined_injured = df_daily_injured
        
    df_combined_injured.to_csv(running_injured_file, index=False)
    print(f"Updated '{running_injured_file}'. Total rows: {len(df_combined_injured)}")

if __name__ == "__main__":
    run_daily_injury_adjustment()