import os

html_path = r'c:\Users\user\Desktop\Bus-tracker\templates\index.html'

with open(html_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Wrap the exact duplicated code into Jinja2 mobile and desktop conditionals
new_content = "{% if is_mobile %}\n" + content + "\n{% else %}\n" + content + "\n{% endif %}\n"

with open(html_path, 'w', encoding='utf-8') as f:
    f.write(new_content)

print("HTML template successfully split into Mobile and PC blocks.")
