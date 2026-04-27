"""
Test: upload a file to the Bambu printer via implicit FTPS and verify integrity.
Usage:  python test_ftps.py <IP> <ACCESS_CODE> [filepath]
        Without filepath: uploads a 1KB test payload.
        With filepath:    uploads the specified file (e.g. model.3mf).
"""
import sys
import os
import ssl
import socket
import io
from ftplib import FTP_TLS


class ImplicitFTPS(FTP_TLS):
    """FTP_TLS with immediate TLS handshake (implicit FTPS, port 990)."""

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        if host:
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if source_address is not None:
            self.source_address = source_address

        self.sock = socket.create_connection(
            (self.host, self.port), self.timeout
        )
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(
            self.sock, server_hostname=self.host
        )
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        """Upload with TLS-safe data channel close (avoids deadlock)."""
        self.voidcmd("TYPE I")
        conn = self.transfercmd(cmd, rest)
        try:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
        finally:
            if isinstance(conn, ssl.SSLSocket):
                conn.settimeout(10)
                try:
                    raw = conn.unwrap()
                    raw.close()
                except (ssl.SSLError, OSError, TimeoutError):
                    try:
                        conn.close()
                    except OSError:
                        pass
            else:
                conn.close()
        return self.voidresp()


def main():
    if len(sys.argv) < 3:
        print("Usage: python test_ftps.py <PRINTER_IP> <ACCESS_CODE> [filepath]")
        sys.exit(1)

    ip = sys.argv[1]
    access_code = sys.argv[2]
    filepath = sys.argv[3] if len(sys.argv) > 3 else None

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    print(f"[1] Connecting to {ip}:990 (implicit FTPS) ...")
    ftp = ImplicitFTPS(context=ctx)
    try:
        ftp.connect(host=ip, port=990, timeout=60)
        print(f"[OK] Connected! Banner: {ftp.welcome}")

        print("[2] Logging in ...")
        ftp.login(user="bblp", passwd=access_code)
        print("[OK] Login successful!")

        print("[3] Enabling data-channel protection (PROT P) ...")
        ftp.prot_p()
        print("[OK] PROT P OK!")

        if filepath:
            # Upload a real file
            filename = os.path.basename(filepath)
            local_size = os.path.getsize(filepath)
            print(f"[4] Uploading '{filename}' ({local_size} bytes) ...")
            with open(filepath, "rb") as f:
                ftp.storbinary(f"STOR {filename}", f)
            print("[OK] Upload complete!")

            # Verify size
            print("[5] Verifying file size on printer ...")
            try:
                remote_size = ftp.size(filename)
                if remote_size == local_size:
                    print(f"[OK] Size verified: {remote_size} bytes (match!)")
                else:
                    print(f"[FAIL] Size MISMATCH: local={local_size}, remote={remote_size}")
            except Exception as e:
                print(f"[WARN] SIZE command failed: {e}")

        else:
            # Upload a small test payload
            print("[4] Uploading test file (1KB) ...")
            test_data = b"BAMBU_FTPS_TEST " * 64
            ftp.storbinary("STOR _ftps_test.tmp", io.BytesIO(test_data))
            print("[OK] Upload successful!")

            print("[5] Verifying ...")
            try:
                remote_size = ftp.size("_ftps_test.tmp")
                print(f"[OK] Remote size: {remote_size} bytes (expected {len(test_data)})")
            except Exception as e:
                print(f"[WARN] SIZE command failed: {e}")

            print("[6] Cleaning up ...")
            ftp.delete("_ftps_test.tmp")
            print("[OK] Test file deleted.")

        print("[*] Final file listing ...")
        files = ftp.nlst()
        print(f"[OK] Files: {files}")

        ftp.quit()
        print("\n[SUCCESS] Done!")
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
