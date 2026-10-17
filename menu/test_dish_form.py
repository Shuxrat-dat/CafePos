from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from inventory.models import Product

from .models import Category, Dish

User = get_user_model()


class DishFormScreenTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="own_df", password="x", role="owner")
        self.dish = Dish.objects.create(category=Category.objects.create(name="Еда"), name="Плов", price=40000,
                                        allergens="gluten,spicy")
        self.client.force_login(self.owner)
        self.url = reverse("dish_edit", args=[self.dish.id])

    def test_empty_warehouse_explains_what_to_do_instead_of_empty_dropdowns(self):
        response = self.client.get(self.url)
        self.assertContains(response, "На складе пока нет продуктов")
        self.assertContains(response, reverse("product_create"))

    def test_with_products_the_recipe_rows_are_usable(self):
        Product.objects.create(name="Рис", unit="g")
        response = self.client.get(self.url)
        self.assertNotContains(response, "На складе пока нет продуктов")
        self.assertContains(response, "Выберите продукт")
        self.assertContains(response, "Рис")

    def test_allergens_are_separate_labelled_checkboxes_with_saved_ones_checked(self):
        html = self.client.get(self.url).content.decode()
        self.assertEqual(html.count('name="allergens"'), 9)
        checked = {
            code for code in ("gluten", "milk", "spicy", "nuts")
            if any(f'value="{code}"' in tag and "checked" in tag
                   for tag in __import__("re").findall(r"<input[^>]+name=\"allergens\"[^>]*>", html))
        }
        self.assertEqual(checked, {"gluten", "spicy"})
        self.assertContains(self.client.get(self.url), "Для гостей и кухни")
