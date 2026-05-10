from flask import Flask, request, jsonify

app = Flask(__name__)

# This holds the last location in memory
last_location = {"lat": 28.4595, "lng": 77.0266, "time": "Waiting..."}

@app.route('/')
def home():
    return f"Bus is currently at: {last_location['lat']}, {last_location['lng']}"

@app.route('/log', methods=['GET'])
def update():
    global last_location
    lat = request.args.get('lat')
    lng = request.args.get('lon')  
    time = request.args.get('time')

    if lat and lng:
        last_location['lat'] = lat
        last_location['lng'] = lng
        last_location['time'] = time or "N/A"
        print(f"📍 Received: Lat {lat}, Lon {lng}")
        return "OK", 200
    
    return "No Data", 400

@app.route('/get_bus')
def get_bus():
    return jsonify(last_location)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)