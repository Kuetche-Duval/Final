import base64
import json

# Replace 'path/to/your/service-account.json' with the actual file path
file_path = 'service_account.json'

try:
    with open(file_path, 'rb') as file:
        # Read the binary data from the file
        file_data = file.read()
    
    # Encode the binary data to Base64
    encoded_bytes = base64.b64encode(file_data)
    
    # Convert the encoded bytes to a UTF-8 string
    encoded_string = encoded_bytes.decode('utf-8')

    print("Base64 Encoded Service Account Credential:")
    print(encoded_string)
    
except FileNotFoundError:
    print(f"Error: The file at '{file_path}' was not found.")
except Exception as e:
    print(f"An error occurred: {e}")