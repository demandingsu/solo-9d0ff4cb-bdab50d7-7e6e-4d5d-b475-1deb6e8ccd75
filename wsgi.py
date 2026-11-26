"""生产入口：waitress 托管 Flask 应用。"""

from app import create_app

application = create_app()

if __name__ == "__main__":
    from waitress import serve

    serve(application, host="0.0.0.0", port=8000)
