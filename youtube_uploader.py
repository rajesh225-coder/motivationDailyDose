import cloudinary
import cloudinary.api
import cloudinary.uploader
import random
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import googleapiclient.discovery
import google.auth
import os
import pickle
import requests
import json
import subprocess

# --- Cloudinary Configuration (Using GitHub Secrets) ---
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True
)

# --- YouTube API Configuration ---
# GitHub Actions par client_secret.json file ko dynamically banayenge
CLIENT_SECRETS_FILE = "client_secret.json"
# token.pickle file GitHub Actions runner par banegi/use hogi
TOKEN_FILE = 'token.pickle'

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"

def get_authenticated_service():
    """
    YouTube API ke liye authentication handle karta hai.
    GitHub Actions environment mein refresh token ka upyog karta hai.
    """
    credentials = None
    
    # Koshish karein ki token.pickle se credentials load ho jayein
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, 'rb') as token:
                credentials = pickle.load(token)
            print(f"Credentials loaded from {TOKEN_FILE}.")
        except Exception as e:
            print(f"Error loading token.pickle: {e}. Attempting new authorization.")
            # Corrupt file ho sakti hai, delete karein
            try:
                os.remove(TOKEN_FILE) 
            except OSError: # Handle case where file might not exist after all
                pass 
            credentials = None

    # Agar credentials nahi hain ya invalid hain
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            print("Access token expired, attempting to refresh with stored refresh token...")
            try:
                credentials.refresh(Request())
                print("Access token refreshed successfully.")
            except Exception as e:
                print(f"Error refreshing token: {e}. Full re-authentication needed.")
                credentials = None # Refresh fail hone par naya auth flow
        
        # Agar credentials abhi bhi nahi hain (ya refresh fail ho gaya)
        if not credentials:
            print("No valid credentials found or refresh token failed. Initiating authorization flow from secret...")
            
            # GOOGLE_REFRESH_TOKEN secret se refresh token use karein
            refresh_token_secret = os.environ.get("GOOGLE_REFRESH_TOKEN")
            if not refresh_token_secret:
                raise ValueError("GOOGLE_REFRESH_TOKEN GitHub Secret is missing or empty.")
            
            try:
                with open(CLIENT_SECRETS_FILE, 'r') as f:
                    client_config = json.load(f)
                
                web_config = client_config.get("web") or client_config.get("installed")
                if not web_config:
                    raise ValueError("client_secret.json must contain 'web' or 'installed' client configuration.")

                credentials = google.oauth2.credentials.Credentials(
                    token=None,  # Abhi koi access token nahi hai
                    refresh_token=refresh_token_secret,
                    token_uri=web_config.get("token_uri"),
                    client_id=web_config.get("client_id"),
                    client_secret=web_config.get("client_secret"),
                    scopes=SCOPES
                )
                
                # Turant ek valid access token prapt karne ke liye refresh karein
                credentials.refresh(Request())
                print("Initial credentials created and refreshed using GOOGLE_REFRESH_TOKEN secret.")

            except Exception as e:
                print(f"FATAL: Could not establish credentials using GOOGLE_REFRESH_TOKEN secret: {e}")
                print("Please ensure GOOGLE_REFRESH_TOKEN and GOOGLE_CLIENT_SECRETS are correctly set in GitHub Secrets.")
                raise # Error hone par workflow ko fail karein
                
    # Credentials ko save karein future ke runs ke liye
    with open(TOKEN_FILE, 'wb') as token:
        pickle.dump(credentials, token)
    print(f"Credentials saved/updated to {TOKEN_FILE}.")
            
    return googleapiclient.discovery.build(
        API_SERVICE_NAME, API_VERSION, credentials=credentials)

def download_file(url, local_path):
    """Downloads a file from a URL to a local path."""
    print(f"Downloading {url} to {local_path}...")
    with requests.get(url, stream=True) as r:
        r.raise_for_status() # HTTP errors (4xx, 5xx) ko handle karein
        with open(local_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    print("Download complete.")

def merge_video_audio_ffmpeg(video_input_path, audio_input_path, output_path):
    """
    Merges video (muting original audio) with background music using FFmpeg.
    Ensures video is trimmed to the shortest duration (video or audio).
    """
    print(f"Merging video: {video_input_path} with audio: {audio_input_path} into {output_path}...")
    
    # FFmpeg command:
    # -i video_input_path: Input video
    # -i audio_input_path: Input audio
    # -map 0:v: Map only video stream from first input (input 0)
    # -map 1:a: Map only audio stream from second input (input 1)
    # -c:v copy: Copy video stream without re-encoding (fast and no quality loss)
    # -shortest: Output duration is determined by the shortest input stream (video or audio)
    # -y: Overwrite output file if it exists
    ffmpeg_command = [
        "ffmpeg",
        "-i", video_input_path,
        "-i", audio_input_path,
        "-map", "0:v",
        "-map", "1:a",
        "-c:v", "copy",
        "-shortest",
        "-y", # Overwrite output file if it exists
        output_path
    ]

    try:
        # Run FFmpeg command
        result = subprocess.run(ffmpeg_command, check=True, capture_output=True, text=True)
        print("FFmpeg stdout:")
        print(result.stdout)
        print("FFmpeg stderr:")
        print(result.stderr)
        print(f"Video and audio merged successfully to {output_path}")
        return output_path
    except subprocess.CalledProcessError as e:
        print(f"FFmpeg command failed with error code {e.returncode}")
        print("FFmpeg stdout:")
        print(e.stdout)
        print("FFmpeg stderr:")
        print(e.stderr)
        raise Exception(f"Video/audio merging failed: {e}")
    except FileNotFoundError:
        print("FFmpeg not found. Please ensure FFmpeg is installed and accessible in your PATH.")
        raise Exception("FFmpeg not found.")


def upload_video_to_youtube(youtube_service, video_file_path, title, description, tags):
    """
    Local file se YouTube par video upload karta hai.
    """
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "22" # Video category ID (e.g., 22 for People & Blogs)
        },
        "status": {
            "privacyStatus": "public" # public, private, ya unlisted
        }
    }

    print(f"Uploading '{title}' to YouTube...")
    insert_request = youtube_service.videos().insert(
        part="snippet,status",
        body=body,
        media_body=googleapiclient.http.MediaFileUpload(video_file_path)
    )
    response = insert_request.execute()
    print(f"Video successfully uploaded! Video ID: {response.get('id')}")
    print(f"YouTube URL: https://www.youtube.com/watch?v={response.get('id')}")
    return response.get('id') # Return YouTube video ID

def main():
    """
    Main function jo Cloudinary se video aur music fetch karke merge karta hai,
    aur phir merged video ko YouTube par upload karta hai.
    """
    # Define temporary file paths
    temp_video_path = "temp_video.mp4"
    temp_music_path = "temp_music.mp3" # Keep .mp3 extension for music
    merged_output_path = "merged_output.mp4"

    # Initialize youtube_service outside the main try block
    youtube_service = None 

    try:
        # --- Authentication Service ko Yahan Call Karein ---
        # Isse youtube_service har haal mein define ho jayega agar authentication successful hai
        youtube_service = get_authenticated_service() 

        # --- 1. Cloudinary se random video fetch karein ('Quotes_Videos' folder se, jo upload nahi hui ho) ---
        print("Searching for un-uploaded videos in Cloudinary 'Quotes_Videos' folder...")
        search_results = cloudinary.Search()\
            .expression("resource_type:video AND folder:Quotes_Videos AND -tags:uploaded_to_youtube")\
            .sort_by("public_id", "asc")\
            .max_results(500)\
            .execute()
        
        videos = search_results.get('resources', [])
        
        if not videos:
            print("No un-uploaded videos found in Cloudinary 'Quotes_Videos' folder. Exiting.")
            return

        random_video = random.choice(videos)
        video_url = random_video.get('secure_url')
        video_public_id = random_video.get('public_id')
        print(f"Selected random video: {video_public_id}, URL: {video_url}")

        # --- 2. Direct Background Music URL ka upyog karein ---
        # Aapne jo link diya hai use yahan hardcode kar dein.
        music_url = "https://res.cloudinary.com/decqrz2gm/video/upload/v1750532138/backmusic/Control_isn_t_....mp3"
        # Music ka public ID is URL se nikal sakte hain agar description mein use karna hai.
        # Ya aap ise manually bhi define kar sakte hain agar yeh fixed hai.
        music_public_id = "backmusic/Control_isn_t_...." # Example public_id for description if needed
        print(f"Using fixed background music URL: {music_url}")

        # --- 3. Video aur Music Files Download Karein ---
        download_file(video_url, temp_video_path)
        download_file(music_url, temp_music_path) 
        
        # --- 4. Video aur Audio ko Merge Karein (FFmpeg ka upyog karke) ---
        merged_video_path = merge_video_audio_ffmpeg(temp_video_path, temp_music_path, merged_output_path)

        # --- 5. YouTube metadata (Motivational Content) ---
        motivational_titles = [
            "Unleash Your Inner Power: A Motivational Journey!",
            "Believe in Yourself: The Path to Success Starts Now!",
            "Never Give Up: Find Your Drive & Conquer Your Goals!",
            "Daily Dose of Motivation: Fuel Your Dreams!",
            "Inspire Your Day: Positive Vibes & Strong Mindset!",
            "Push Your Limits: Transform Your Life Today!",
            "The Power of Positive Thinking: Achieve Anything!",
            "Wake Up & Win: Your Morning Motivation Boost!",
            "Success Mindset: Build Your Empire!",
            "Stay Focused, Stay Strong: Your Ultimate Motivation!"
        ]
        
        youtube_title = random.choice(motivational_titles) 

        youtube_description = (
            "Welcome to our channel! This video is designed to ignite your inner fire and keep you motivated on your journey to success. "
            "Remember, every challenge is an opportunity in disguise. Believe in yourself, stay consistent, and never stop chasing your dreams.\n\n"
            "If you found this video inspiring, please like, share, and subscribe for more motivational content!\n\n"
            
            "--- Music & Copyright --- \n"       
            "I do not claim ownership of the background music used in this video. This video is for motivational and entertainment purposes only.\n\n"
       
            "--- Searching Tags --- \n"
            "#Motivation #Inspiration #Success #BelieveInYourself #NeverGiveUp #PositiveVibes #Mindset #GoalSetting #DreamBig #SelfImprovement #MotivationalVideo #LifeHacks #Productivity #StayStrong #AchieveGoals #DailyMotivation #FitnessMotivation #StudyMotivation #WorkMotivation #InspirationalQuotes #Focus"
        )
        
        youtube_tags = [
            "motivation", "inspiration", "success", "believe in yourself", 
            "never give up", "positive vibes", "mindset", "goal setting", 
            "dream big", "self improvement", "motivational video", 
            "daily motivation", "inspirational quotes", "focus",
            "personal growth", "achieve goals", "productivity tips"
        ]

        # --- 6. YouTube par merged video upload karein ---
        youtube_video_id = upload_video_to_youtube(youtube_service, merged_video_path, youtube_title, youtube_description, youtube_tags)
        
        # --- 7. Cloudinary mein video ko 'uploaded_to_youtube' tag karein ---
        if youtube_video_id:
            print(f"Tagging Cloudinary video '{video_public_id}' as 'uploaded_to_youtube'...")
            cloudinary.uploader.add_tag("uploaded_to_youtube", video_public_id, resource_type="video")
            print("Cloudinary video tagged successfully.")

    except Exception as e:
        print(f"Ek error aa gaya: {e}")
        raise # Error hone par GitHub Action job ko fail karein
    finally:
        # --- 8. Temporary files ko delete karein (cleanup) ---
        for f_path in [temp_video_path, temp_music_path, merged_output_path]:
            if os.path.exists(f_path):
                try:
                    os.remove(f_path)
                    print(f"Cleaned up temporary file: {f_path}")
                except OSError as e:
                    print(f"Error cleaning up file {f_path}: {e}")

if __name__ == "__main__":
    main()
