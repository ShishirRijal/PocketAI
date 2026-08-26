Choose the category for this transaction.

Transaction: {{transaction}}
Original message: "{{text}}"

The user's categories (id: name):
{{category_list}}

Return `category_id` if one of them fits reasonably well. Only if none fits, return
`new_category_name` (short, Title Case, may use "Parent/Child" like "Documents/Admin")
and leave `category_id` null. Creating categories is expensive for the user, so prefer
existing ones when the fit is decent.
