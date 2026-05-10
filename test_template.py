from flask import Flask, render_template

app = Flask(__name__)

@app.route('/')
def test():
    return render_template('index.html')

if __name__ == '__main__':
    with app.app_context():
        try:
            print("Rendering template...")
            html = render_template('index.html')
            print("Template rendered successfully!")
        except Exception as e:
            print("Exception during rendering:", type(e).__name__, str(e))
