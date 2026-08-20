
import time
import pypdfium2 as pdfium

def main():
    target_file = r"testfiler\0711781315.pdf"
    password = "0711781315"
    
    print(f"Diagnosing: {target_file}")
    
    # 1. Test correctness
    try:
        pdf = pdfium.PdfDocument(target_file, password=password)
        print("Password OK!")
    except Exception as e:
        print(f"Password FAILED: {e}")
        return

    # 2. Test speed
    iterations = 100
    print(f"Testing {iterations} iterations...")
    start = time.time()
    for _ in range(iterations):
        try:
            pdfium.PdfDocument(target_file, password="wrong")
        except:
            pass
    duration = time.time() - start
    speed = iterations / duration
    print(f"Speed: {speed:.1f} checks/sec")

if __name__ == "__main__":
    main()
