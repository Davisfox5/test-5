from flask import Flask, render_template
import random
import string

app = Flask(__name__)

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/generate')
def generate():
    random_word = ''.join(random.choices(string.ascii_lowercase + string.ascii_uppercase, k=10))
    return render_template('generate.html', word=random_word)

if __name__ == "__main__":
    app.run(debug=True)