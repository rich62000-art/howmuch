from db import (
    rebuild_apt_sale_list,
    rebuild_rent_list,
    rebuild_presale_list,
)

print("=== rebuild test start ===")

rebuild_apt_sale_list()
rebuild_rent_list()
rebuild_presale_list()

print("=== rebuild test complete ===")