package com.autoimmune.orders;

import java.math.BigDecimal;

public record Item(String name, BigDecimal price, int qty) {
}
