from app import create_server


if __name__ == "__main__":
    server = create_server()
    print("无人机适航管理服务已启动", flush=True)
    server.serve_forever()
