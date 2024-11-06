import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import uuid
from redis import Redis
from rq import Queue
from background_tasks import process_submission
import time
import logging
from collections import Counter


app = Flask(__name__)
CORS(app)  # Enable CORS

# Set up Redis connection
redis_conn = Redis()

# Create an RQ queue
queue = Queue('submission_queue', connection=redis_conn)

# Load rate limits dynamically from environment variables or fallback to defaults
RATE_LIMITS = {
    'free': {
        'per_minute': int(os.getenv('FREE_RATE_PER_MINUTE', 10)),
        'per_hour': int(os.getenv('FREE_RATE_PER_HOUR', 100)),
    },
    'standard': {
        'per_minute': int(os.getenv('STANDARD_RATE_PER_MINUTE', 50)),
        'per_hour': int(os.getenv('STANDARD_RATE_PER_HOUR', 500)),
    },
    'premium': {
        'per_minute': int(os.getenv('PREMIUM_RATE_PER_MINUTE', 200)),
        'per_hour': int(os.getenv('PREMIUM_RATE_PER_HOUR', 2000)),
    }
}

# Helper function to check rate limits using token bucket
def is_rate_limited(user_id, tier):
    limits = RATE_LIMITS.get(tier, RATE_LIMITS['free'])  # Default to free tier limits if not found
    current_time = int(time.time())

    minute_key = f"rate_limit:{user_id}:{tier}:minute"
    hour_key = f"rate_limit:{user_id}:{tier}:hour"

    # Increment request counts in Redis
    minute_count = redis_conn.incr(minute_key)
    hour_count = redis_conn.incr(hour_key)

    # Set expiration if it's the first request
    if minute_count == 1:
        redis_conn.expire(minute_key, 60)  # 1 minute
    if hour_count == 1:
        redis_conn.expire(hour_key, 3600)  # 1 hour

    # If limits are exceeded, return retry information
    if minute_count > limits['per_minute'] or hour_count > limits['per_hour']:
        retry_in_seconds = redis_conn.ttl(minute_key)
        return True, retry_in_seconds
    return False, None

@app.route('/api/v1/analytics/submit', methods=['POST'])
def submit_data():
    # Parse incoming request
    data = request.get_json()
    platform = data.get('platform')
    content = data.get('content')
    timestamp = data.get('timestamp')
    user_id = request.headers.get('X-User-ID')  # Assume user ID is passed in headers
    tier = request.headers.get('X-User-Tier')  # Get the user tier from headers

    if not user_id:
        return jsonify({'status': 'error', 'message': 'Missing X-User-ID header.'}), 400

    if not tier or tier not in RATE_LIMITS:
        return jsonify({'status': 'error', 'message': 'Invalid or missing X-User-Tier header.'}), 400

    # Check if user is rate limited
    is_limited, retry_after = is_rate_limited(user_id, tier)
    if is_limited:
        response = jsonify({'status': 'error', 'message': 'Rate limit exceeded. Please try again later.'})
        response.headers['Retry-After'] = retry_after
        return response, 429

    # Generate unique submission ID
    submission_id = str(uuid.uuid4())
    timestamp = int(time.time())  # Use the current time or provided timestamp

    # Define the user's submissions key
    user_submissions_key = f"user:{user_id}:submissions"

    # Add submission to the user's submissions set
    redis_conn.zadd(user_submissions_key, {submission_id: timestamp})

    # Store the submission details
    redis_conn.hmset(f"submission:{submission_id}", {
        'platform': platform,
        'hashtags': data.get('hashtags', ''),  # Make sure to capture hashtags if provided
        'sentiment_score': data.get('sentiment_score', 0.0)  # Capture sentiment score if provided
    })

    # Return successful response
    return jsonify({'status': 'Data received successfully', 'submission_id': submission_id}), 200


@app.route('/api/v1/analytics/dashboard', methods=['GET'])
def get_dashboard():
    # Parse query parameters
    user_id = request.args.get('user_id')
    platform = request.args.get('platform')  # Optional
    start_time = request.args.get('start_time')  # Optional
    end_time = request.args.get('end_time')  # Optional

    # Validate user_id
    if not user_id:
        return jsonify({'status': 'error', 'message': 'Missing user_id query parameter.'}), 400

    # Validate timestamps if provided
    try:
        start_epoch = int(time.mktime(time.strptime(start_time, "%Y-%m-%dT%H:%M:%SZ"))) if start_time else 0
        end_epoch = int(time.mktime(time.strptime(end_time, "%Y-%m-%dT%H:%M:%SZ"))) if end_time else int(time.time())
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Invalid time format. Use ISO 8601 format.'}), 400

    if start_epoch > end_epoch:
        return jsonify({'status': 'error', 'message': 'start_time cannot be greater than end_time.'}), 400

    # Define user's submissions key
    user_submissions_key = f"user:{user_id}:submissions"

    # Retrieve submission IDs within the time range
    submission_ids = redis_conn.zrangebyscore(user_submissions_key, start_epoch, end_epoch)

    mentions_count = len(submission_ids)
    hashtags_counter = Counter()
    sentiment_total = 0.0
    sentiment_count = 0

    for submission_id in submission_ids:
        submission_key = f"submission:{submission_id.decode('utf-8')}"
        submission = redis_conn.hgetall(submission_key)

        if not submission:
            continue  # Skip if submission data is missing

        submission = {k.decode('utf-8'): v.decode('utf-8') for k, v in submission.items()}

        if platform and submission.get('platform') != platform:
            continue

        # Count hashtags
        hashtags = submission.get('hashtags', "")
        if hashtags:
            hashtags_counter.update(hashtags.split(","))

        # Accumulate sentiment scores
        sentiment = float(submission.get('sentiment_score', 0.0))
        sentiment_total += sentiment
        sentiment_count += 1

    # Calculate top hashtags
    top_hashtags = [tag for tag, count in hashtags_counter.most_common()]  # All popular hashtags

    # Calculate average sentiment score
    sentiment_score = (sentiment_total / sentiment_count) if sentiment_count > 0 else 0.0

    return jsonify({
        'mentions_count': mentions_count,
        'top_hashtags': top_hashtags,
        'sentiment_score': sentiment_score
    }), 200

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    app.run(debug=True)
