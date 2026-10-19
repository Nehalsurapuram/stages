package com.autoimmune.orders;

import java.util.List;

public record Order(long id, String customer, List<Item> items) {
}
